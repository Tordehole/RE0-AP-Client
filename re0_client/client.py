from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import logging
import sys
import os
import time
import tempfile
import struct
import json
import re
import base64
from collections import deque
from dataclasses import dataclass
from typing import Optional

import Utils
from CommonClient import CommonContext, ClientCommandProcessor, get_base_parser, gui_enabled, server_loop
from NetUtils import NetworkItem, ClientStatus

from .client_data import ITEM_DEFS, LOCATION_DEFS, PICKUP_DEFS

logger = logging.getLogger("RE0Client")

# Full Normal integration: all inventory delivery is performed directly from
# Python via WriteProcessMemory. No injected ASI/DLL bridge is required.
ITEM_BY_AP_ID = {item.code: item for item in ITEM_DEFS}
AP_TO_RE0 = {
    item.code: (item.re0_item_id, item.re0_qty, item.slots)
    for item in ITEM_DEFS
}
LOCATION_CODE_BY_NAME = {location.name: location.code for location in LOCATION_DEFS}
INK_RIBBON_LOCATION_IDS = {
    location.code for location in LOCATION_DEFS
    if location.vanilla_item.startswith("Ink Ribbon")
}
GOAL_PROOF_LOCATION_NAME = "Facility - Large Hall - Fire Key"
GOAL_PROOF_LOCATION_ID = LOCATION_CODE_BY_NAME[GOAL_PROOF_LOCATION_NAME]

# Corrected Steam-build pointers validated against the final-boss victory probe.
ROOM_NEXT_PTR = 0x00DCBEB8
MENU_ID_PTR = 0x00E2F688


# ---- v0.0.37 dropped progression-item fix ---------------
#
# v0.0.33 proved the broad manager-record approach works, but the full run
# exposed four timing/classification cases:
#   1) some native room-spawn records legitimately use link=FFFFFFFF;
#   2) AP GIVE inventory gains can look like floor-pickup gains;
#   3) a native record can retire just before the inventory gain is sampled;
#   4) Edward's ammo and the Conductor's Office briefcase are scripted pickups
#      with no matching type-0x71 retirement in the observed manager table.
#
# v0.0.34 keeps the successful static-vs-dropped lifecycle, adds native preload
# learning, correlates player drops with inventory loss, ignores expected AP
# GIVE gains, supports retirement-first ordering, and adds a conservative
# scripted-pickup fallback after the dynamic-object window has elapsed.

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ROOM_ROOT_PTR = 0x00DCC070
INV_ROOT_PTR = 0x00DCBF44
WORLD_MANAGER_PTR = 0x00DCC010
ROOM_ID_OFFSET = 0x1FD4
INV_REBECCA_OFFSET = 0x24
INV_BILLY_OFFSET = 0x64
SLOTS_PER_CHARACTER = 6

INVENTORY_SWITCH = 0x005E1E80
UNPACKED_SIGNATURE = b"\xff\x24\x85\x94\x57\x5e\x00"

TABLE_BASE_OFFSET = 0x28
RECORD_COUNT = 0x4E8
RECORD_STRIDE = 0x5C
TABLE_SIZE = RECORD_COUNT * RECORD_STRIDE
WORLD_ITEM_TYPE = 0x71
# Original RE0 static world-item records used by the mapped Normal game end at 588.
# Records from 589 onward are the engine's runtime/drop pool in every capture so far.
DYNAMIC_RECORD_START = 589

PICKUP_MATCH_WINDOW = 5.0
DROP_ACTIVATION_GRACE = 0.85
SCRIPTED_FALLBACK_DELAY = 3.0
EXPECTED_DELIVERY_WINDOW = 3.0

@dataclass(frozen=True)
class PickupLocationSpec:
    location_id: int
    name: str
    room: int
    item_id: int
    vanilla_qty: int
    position: Optional[tuple[float, float, float]] = None
    position_tolerance: float = 2.0
    record_index: Optional[int] = None
    scripted: bool = False
    slots: int = 1

# Known native record signatures below come from live Train captures.
# They are not required for every location: unseen rooms can still be learned
# from pre-load activation before the room becomes current. The signatures are
# an extra guard for native records that use link=FFFFFFFF.
PICKUP_LOCATIONS = tuple(
    PickupLocationSpec(
        LOCATION_CODE_BY_NAME[p.location_name],
        p.location_name,
        p.room,
        p.item_id,
        p.vanilla_qty,
        p.position,
        2.0,
        p.record_index,
        p.scripted,
        p.slots,
    )
    for p in PICKUP_DEFS
)

SPECS_BY_ROOM_ITEM: dict[tuple[int, int], list[PickupLocationSpec]] = {}
SPEC_BY_LOCATION: dict[int, PickupLocationSpec] = {}
ALL_PICKUP_ITEM_IDS: set[int] = set()
for _spec in PICKUP_LOCATIONS:
    SPECS_BY_ROOM_ITEM.setdefault((_spec.room, _spec.item_id), []).append(_spec)
    SPEC_BY_LOCATION[_spec.location_id] = _spec
    ALL_PICKUP_ITEM_IDS.add(_spec.item_id)

@dataclass
class PendingPickup:
    when: float
    character: str
    char_index: int
    slot_index: int
    item_id: int
    gained_qty: int
    before_item: int
    before_qty: int
    after_item: int
    after_qty: int
    before_slot: bytes
    after_slot: bytes
    room: int

@dataclass
class PendingRetirement:
    when: float
    record_index: int
    spec: PickupLocationSpec
    record_fields: dict
    inv_before: bytes | None = None
    inv_after: bytes | None = None

@dataclass
class PendingActivation:
    when: float
    record_index: int
    record_fields: dict
    spec: Optional[PickupLocationSpec]

@dataclass
class RecentLoss:
    when: float
    room: int
    item_id: int
    qty: int

@dataclass
class PendingSaveCandidate:
    when: float
    room: int

@dataclass
class ExpectedAPDelivery:
    when: float
    index: int
    item_id: int
    qty: int

@dataclass
class ExpectedRollbackLoss:
    when: float
    item_id: int
    qty: int

@dataclass
class PendingTwoSlotSuppression:
    queued_at: float
    due_at: float
    expires_at: float
    character: str
    char_index: int
    item_id: int
    item_qty: int
    record_index: int
    spec: PickupLocationSpec
    record_fields: dict
    source: str

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]

class RE0MemoryProbe:
    def __init__(self):
        self.handle = None
        self.pid = None
        self.log_path = os.path.join(tempfile.gettempdir(), "RE0_AP_full_normal_direct_python.log")
        self.disabled_location_ids: set[int] = set()
        self._kernel32 = None
        self._last_table = None
        self._last_inv = None
        self._last_room = None
        self._static_done_pid = None

        self._pending: list[PendingPickup] = []
        self._pending_retirements: list[PendingRetirement] = []
        self._pending_activations: dict[int, PendingActivation] = {}
        self._recent_losses: list[RecentLoss] = []
        self._native_records: dict[int, int] = {}   # record index -> AP location id
        self._dynamic_records: set[int] = set()
        self._expected_deliveries: list[ExpectedAPDelivery] = []
        self._expected_rollback_losses: list[ExpectedRollbackLoss] = []
        self._pending_two_slot: list[PendingTwoSlotSuppression] = []
        self._handled_locations: set[int] = set()
        self._read_failures = 0

        self.pending_ap_checks = deque()
        self.location_character_hint: dict[int, tuple[int, float]] = {}
        self.goal_detected = False
        self._goal_logged = False
        self.load_restore_events = deque()
        self.save_events = deque()
        self._save_candidates: list[PendingSaveCandidate] = []
        self.load_restore_snapshot_callback = None
        self._load_restore_pre: bytes | None = None
        self._load_restore_pre_table: bytes | None = None
        self._load_restore_pre_room: int | None = None
        self._load_restore_pid: int | None = None
        self._load_restore_stable_since: float | None = None
        # v1.11: RE0 can expose a fully populated, apparently stable OLD room for
        # ~10 seconds before the loaded save is actually committed.  Keep the room
        # from before the load and prefer a real room-commit transition over a timer.
        self._load_restore_first_seen: float | None = None
        self._load_restore_populated_since: float | None = None
        self._load_restore_room_commit_seen = False
        self._load_restore_last_signal = None
        self._load_restore_last_room: int | None = None
        self._load_restore_last: bytes | None = None
        self._load_restore_last_table: bytes | None = None

        # v1.9+: first attachment can occur while RE0 is still constructing the
        # loaded game.  During that window the inventory can jump from the game's
        # boot/default values to the save inventory while the world-item table goes
        # from empty to populated.  Do not let that transition masquerade as a pickup.
        self._startup_quarantine = True
        self._startup_stable_since: float | None = None
        self._startup_last_room: int | None = None
        self._startup_last_inv: bytes | None = None
        self._startup_last_table: bytes | None = None
        self._startup_wait_logged = False

        # v1.13: once Billy's inventory bank has ever been observed live, the
        # opening-story safety gate is permanently satisfied for this process.
        # Before then, Conductor's Key deliveries remain queued rather than
        # allowing Rebecca to sequence-break into the Conductor Wing.
        self._billy_joined_seen = False

    def _k32(self):
        if self._kernel32 is not None:
            return self._kernel32
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
        k.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32FirstW.restype = ctypes.wintypes.BOOL
        k.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.restype = ctypes.wintypes.BOOL
        k.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
        k.OpenProcess.restype = ctypes.wintypes.HANDLE
        k.ReadProcessMemory.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
        ]
        k.ReadProcessMemory.restype = ctypes.wintypes.BOOL
        k.WriteProcessMemory.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
        ]
        k.WriteProcessMemory.restype = ctypes.wintypes.BOOL
        k.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        k.CloseHandle.restype = ctypes.wintypes.BOOL
        self._kernel32 = k
        return k

    def set_disabled_location_ids(self, location_ids: set[int]) -> None:
        self.disabled_location_ids = set(location_ids)
        # If the game was already open before AP connected, discard any temporary
        # classification for checks disabled by this seed's options.
        self._native_records = {
            rec: loc for rec, loc in self._native_records.items()
            if loc not in self.disabled_location_ids
        }
        self._handled_locations.difference_update(self.disabled_location_ids)
        self.pending_ap_checks = deque(
            loc for loc in self.pending_ap_checks if loc not in self.disabled_location_ids
        )

    def _append(self, s):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(s)
        except Exception:
            pass

    def _find_pid(self):
        if sys.platform != "win32":
            return None
        k = self._k32()
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return None
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(pe)
            if not k.Process32FirstW(snap, ctypes.byref(pe)):
                return None
            while True:
                if pe.szExeFile.lower() == "re0hd.exe":
                    return int(pe.th32ProcessID)
                if not k.Process32NextW(snap, ctypes.byref(pe)):
                    break
        finally:
            k.CloseHandle(snap)
        return None

    def _connect(self):
        if self.handle:
            return True
        pid = self._find_pid()
        if not pid:
            return False
        if self._load_restore_pre is not None and self._load_restore_pid != pid:
            old_pid = self._load_restore_pid
            self._load_restore_pid = int(pid)
            self._load_restore_stable_since = None
            self._load_restore_first_seen = None
            self._load_restore_populated_since = None
            self._load_restore_room_commit_seen = False
            self._load_restore_last_signal = None
            self._load_restore_last_room = None
            self._load_restore_last = None
            self._load_restore_last_table = None
            self._append(
                f"LOAD_RESTORE REBOUND old_pid={old_pid} new_pid={pid}; preserving pre-crash snapshot.\n"
            )
        access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
        h = self._k32().OpenProcess(access, False, pid)
        if not h:
            return False
        self.handle = h
        self.pid = pid
        self._last_table = None
        self._last_inv = None
        self._last_room = None
        self._pending.clear()
        self._pending_retirements.clear()
        self._pending_activations.clear()
        self._recent_losses.clear()
        self._save_candidates.clear()
        self._native_records.clear()
        self._dynamic_records.clear()
        self._expected_deliveries.clear()
        self._expected_rollback_losses.clear()
        self._pending_two_slot.clear()
        self._handled_locations.clear()
        self._read_failures = 0
        self._startup_quarantine = True
        self._startup_stable_since = None
        self._startup_last_room = None
        self._startup_last_inv = None
        self._startup_last_table = None
        self._startup_wait_logged = False
        self._billy_joined_seen = False
        return True

    def _disconnect(self):
        if self.handle:
            try:
                self._k32().CloseHandle(self.handle)
            except Exception:
                pass
        self.handle = None
        self.pid = None
        self._last_table = None
        self._last_inv = None
        self._last_room = None
        self._pending.clear()
        self._pending_retirements.clear()
        self._pending_activations.clear()
        self._recent_losses.clear()
        self._save_candidates.clear()
        self._native_records.clear()
        self._dynamic_records.clear()
        self._expected_deliveries.clear()
        self._expected_rollback_losses.clear()
        self._pending_two_slot.clear()
        self._handled_locations.clear()
        self._read_failures = 0

    def _read(self, address, size):
        if not self.handle or not address or size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = self._k32().ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got)
        )
        if not ok or got.value != size:
            return None
        return bytes(buf.raw[:size])

    def _write(self, address, data):
        if not self.handle:
            return False
        buf = ctypes.create_string_buffer(data)
        wrote = ctypes.c_size_t(0)
        ok = self._k32().WriteProcessMemory(
            self.handle, ctypes.c_void_p(address), buf, len(data), ctypes.byref(wrote)
        )
        return bool(ok and wrote.value == len(data))

    def _u32(self, address):
        b = self._read(address, 4)
        return None if b is None else int.from_bytes(b, "little")

    def _room(self):
        root = self._u32(ROOM_ROOT_PTR)
        if not root:
            return None
        b = self._read(root + ROOM_ID_OFFSET, 1)
        return None if not b else b[0]

    def _inv_root(self):
        return self._u32(INV_ROOT_PTR) or 0

    def _inventory(self):
        root = self._inv_root()
        if not root:
            return None
        r = self._read(root + INV_REBECCA_OFFSET, 48)
        b = self._read(root + INV_BILLY_OFFSET, 48)
        if r is None or b is None:
            return None
        return r + b

    def _remember_check_character(self, location_id: int, char_index: int) -> None:
        if char_index in (0, 1):
            self.location_character_hint[int(location_id)] = (int(char_index), time.monotonic())

    def preferred_character_for_location(self, location_id: int | None) -> int | None:
        if location_id is None:
            return None
        hint = self.location_character_hint.get(int(location_id))
        if not hint:
            return None
        char_index, when = hint
        if time.monotonic() - when > 15.0:
            return None
        return char_index

    def _deep_read(self, base_addr: int, offsets: list[int], size: int):
        ptr = self._u32(base_addr)
        if not ptr:
            return None
        for off in offsets[:-1]:
            ptr = self._u32(ptr + off)
            if not ptr:
                return None
        return self._read(ptr + offsets[-1], size)

    def _ending_room_next(self):
        raw = self._deep_read(ROOM_NEXT_PTR, [0x20], 4)
        return None if raw is None else int.from_bytes(raw, "little")

    def _ending_menu_id(self):
        raw = self._deep_read(MENU_ID_PTR, [0x94, 0x14, 0x88, 0x14, 0x0C], 1)
        return None if raw is None else raw[0]

    def _goal_tick(self, room: int | None) -> None:
        if self.goal_detected or room != 165:
            return
        nxt = self._ending_room_next()
        menu = self._ending_menu_id()
        if menu == 21 and nxt == 165:
            self.goal_detected = True
            if not self._goal_logged:
                self._goal_logged = True
                self._append(
                    f"\nGOAL_MATCH roomCur=165 roomNext=165 menuId=21 @ {time.strftime('%H:%M:%S')}\n"
                )

    def _arm_load_restore(self) -> None:
        if self._last_inv is None or self.pid is None:
            return
        self._load_restore_pre = bytes(self._last_inv)
        self._load_restore_pre_table = bytes(self._last_table) if self._last_table is not None else None
        self._load_restore_pre_room = int(self._last_room) if self._last_room is not None else None
        self._load_restore_pid = int(self.pid)
        self._load_restore_stable_since = None
        self._load_restore_first_seen = None
        self._load_restore_populated_since = None
        self._load_restore_room_commit_seen = False
        self._load_restore_last_signal = None
        self._load_restore_last_room = None
        self._load_restore_last = None
        self._load_restore_last_table = None
        # These are transaction-local guards, not permanent AP state.  An older
        # save can legitimately respawn an already-checked vanilla record, which
        # must be suppressed again while the server simply ignores the duplicate
        # LocationCheck.
        self._handled_locations.clear()
        self._append(
            f"LOAD_RESTORE ARMED pid={self.pid} pre_room={self._load_restore_pre_room} inv={self._fmt_inv(self._last_inv)}\n"
        )
        if self.load_restore_snapshot_callback is not None:
            try:
                self.load_restore_snapshot_callback(self._load_restore_pre, self._load_restore_pre_table)
            except Exception as exc:
                self._append(f"LOAD_RESTORE SNAPSHOT CALLBACK ERROR {exc!r}\n")

    def seed_load_restore_snapshot(self, inv: bytes, table: bytes | None = None) -> None:
        if inv is None or len(inv) != 96:
            return
        self._load_restore_pre = bytes(inv)
        self._load_restore_pre_table = bytes(table) if table is not None else None
        self._load_restore_pre_room = None
        self._load_restore_pid = int(self.pid) if self.pid is not None else None
        self._load_restore_stable_since = None
        self._load_restore_first_seen = None
        self._load_restore_populated_since = None
        self._load_restore_room_commit_seen = False
        self._load_restore_last_signal = None
        self._load_restore_last_room = None
        self._load_restore_last = None
        self._load_restore_last_table = None
        self._handled_locations.clear()
        self._append(
            f"LOAD_RESTORE SEEDED persistent snapshot inv={self._fmt_inv(inv)}\n"
        )

    def _load_restore_tick(self, inv: bytes, table: bytes, room: int | None = None) -> None:
        if self._load_restore_pre is None:
            return
        if self._load_restore_pid != self.pid:
            self._append(
                f"LOAD_RESTORE REBOUND tick old_pid={self._load_restore_pid} new_pid={self.pid}.\n"
            )
            self._load_restore_pid = int(self.pid) if self.pid is not None else None
            self._load_restore_stable_since = None
            self._load_restore_first_seen = None
            self._load_restore_populated_since = None
            self._load_restore_room_commit_seen = False
            self._load_restore_last_signal = None
            self._load_restore_last_room = None
            self._load_restore_last = None
            self._load_restore_last_table = None

        now = time.monotonic()
        active_world = self._active_world_record_count(table)
        room_next = self._ending_room_next()
        menu_id = self._ending_menu_id()

        if self._load_restore_first_seen is None:
            self._load_restore_first_seen = now
            self._append(
                f"LOAD_RESTORE POSTLOAD_WAIT pid={self.pid} pre_room={self._load_restore_pre_room} "
                f"roomCur={room} roomNext={room_next} menu={menu_id}; waiting for load commit\n"
            )

        signal = (room, room_next, menu_id, active_world >= 20)
        if signal != self._load_restore_last_signal:
            self._load_restore_last_signal = signal
            self._append(
                f"LOAD_RESTORE STATE roomCur={room} roomNext={room_next} menu={menu_id} "
                f"active_world={active_world} pre_room={self._load_restore_pre_room}\n"
            )

        # Do not start a completion timer while the global world table is still in
        # RE0's empty/boot state.  This is the same half-loaded state that caused
        # the v1.8 startup phantom check.
        if active_world < 20:
            self._load_restore_populated_since = None
            self._load_restore_stable_since = None
            self._load_restore_last_room = room
            self._load_restore_last = bytes(inv)
            self._load_restore_last_table = bytes(table)
            return

        if self._load_restore_populated_since is None:
            self._load_restore_populated_since = now
            self._append(
                f"LOAD_RESTORE WORLD_READY roomCur={room} roomNext={room_next} "
                f"active_world={active_world}\n"
            )

        # Strong completion signal: the current room finally leaves the room that
        # was active when the reload began.  The v1.10 failure sat in room 47 with
        # a fully populated table for ~10 seconds, then committed the actual save
        # to room 36 and only then applied the final Ink Ribbon 3->2 normalization.
        if (
            not self._load_restore_room_commit_seen
            and self._load_restore_pre_room is not None
            and room is not None
            and room != self._load_restore_pre_room
        ):
            self._load_restore_room_commit_seen = True
            self._load_restore_stable_since = now
            self._load_restore_last_room = room
            self._load_restore_last = bytes(inv)
            self._load_restore_last_table = bytes(table)
            self._append(
                f"LOAD_RESTORE ROOM_COMMIT {self._load_restore_pre_room}->{room} "
                f"roomNext={room_next} menu={menu_id}; waiting for final stable tail\n"
            )
            return

        changed = (
            self._load_restore_last != inv
            or self._load_restore_last_table != table
            or self._load_restore_last_room != room
        )
        if changed:
            self._load_restore_last = bytes(inv)
            self._load_restore_last_table = bytes(table)
            self._load_restore_last_room = room
            self._load_restore_stable_since = now
            return
        if self._load_restore_stable_since is None:
            self._load_restore_stable_since = now
            return

        # Different-room reloads use the real room commit and need only a short
        # stable tail.  Same-room loads cannot provide that signal, so retain a
        # conservative automatic fallback.  This is internal; the player should
        # not need a README instruction to count seconds before playing.
        if self._load_restore_room_commit_seen:
            ready_reason = "room-commit"
            if now - self._load_restore_stable_since < 1.25:
                return
        else:
            populated_age = now - self._load_restore_populated_since
            if populated_age < 15.0:
                return
            if now - self._load_restore_stable_since < 2.5:
                return
            ready_reason = "same-room-fallback"

        pre = self._load_restore_pre
        post = bytes(inv)
        pre_table = self._load_restore_pre_table
        post_table = bytes(table)
        self.load_restore_events.append((pre, post, pre_table, post_table))
        self._append(
            f"LOAD_RESTORE STABLE pid={self.pid} reason={ready_reason} room={room} "
            f"roomNext={room_next} menu={menu_id} "
            f"settled_for={now - self._load_restore_stable_since:.2f}s "
            f"populated_age={now - self._load_restore_populated_since:.2f}s "
            f"pre={self._fmt_inv(pre)} post={self._fmt_inv(post)}\n"
        )
        self._load_restore_pre = None
        self._load_restore_pre_table = None
        self._load_restore_pre_room = None
        self._load_restore_pid = None
        self._load_restore_stable_since = None
        self._load_restore_first_seen = None
        self._load_restore_populated_since = None
        self._load_restore_room_commit_seen = False
        self._load_restore_last_signal = None
        self._load_restore_last_room = None
        self._load_restore_last = None
        self._load_restore_last_table = None

    def direct_deliver(
        self, index: int, item_id: int, qty: int, slots: int = 1, preferred_char: int | None = None
    ) -> tuple[str, str, int | None]:
        """Insert one AP item directly into RE0 inventory.

        The character who triggered the source check is preferred whenever known.
        This mirrors the old bridge's active-character behavior for solo play while
        avoiding any injected module. Items remain pending if that inventory lacks
        enough contiguous space.
        """
        if not self.handle or self._last_inv is None:
            return "WAIT", "gameplay not stable", None
        if self._load_restore_pre is not None:
            return "WAIT", "save/load is still settling", None
        root = self._inv_root()
        room = self._room()
        if not root or room is None:
            return "WAIT", "inventory unavailable", None

        inv = self._inventory()
        if inv is None:
            return "WAIT", "inventory unreadable", None

        billy_live = any(
            self._slot_vals(self._slot(inv, 1, si))[0] != 0
            for si in range(6)
        )
        if billy_live and not self._billy_joined_seen:
            self._billy_joined_seen = True
            self._append("BILLY JOIN GATE OPEN inventory bank is live; Conductor's Key may now deliver.\n")

        # v1.13 train story safety.  Conductor's Key normally belongs after Billy
        # has joined.  Receiving it early can let Rebecca enter the Conductor Wing
        # before the partner/story state is initialized, which can hang subsequent
        # room loads.  Keep the AP item pending until Billy's inventory is live.
        if int(item_id) == 65 and not self._billy_joined_seen:
            return "WAIT", "Conductor's Key pending - waiting for Billy to join", None

        if preferred_char in (0, 1):
            # Strict source-character delivery. Never spill a local pickup reward
            # into the partner's inventory just because the checking character is
            # full. This is important during character-split sections: Billy's
            # inventory can still contain items in memory while he is unavailable.
            # If the checking character has no space, keep the AP item queued.
            order = [preferred_char]
        else:
            # A received item from another AP player has no local source-check
            # character to identify who is currently controlled. Until we have a
            # separately validated active-character flag, keep the conservative
            # one-bank behavior instead of silently spilling into the partner.
            order = [0]
        marker = struct.pack("<II", 180, 1)
        payload = struct.pack("<II", int(item_id), int(qty))

        for ci in order:
            char_off = INV_REBECCA_OFFSET if ci == 0 else INV_BILLY_OFFSET
            if slots == 2:
                candidate = None
                for si in range(5):
                    a = self._slot_vals(self._slot(inv, ci, si))
                    b = self._slot_vals(self._slot(inv, ci, si + 1))
                    if a[0] == 0 and b[0] == 0:
                        candidate = si
                        break
                if candidate is None:
                    continue
                addr = root + char_off + candidate * 8
                old = self._read(addr, 16)
                if old is None:
                    return "WAIT", "target slots unreadable", ci
                self.expect_ap_delivery(index, item_id, qty)
                if not self._write(addr, payload + marker):
                    self.cancel_expected_delivery(index)
                    return "ERR", "two-slot write failed", ci
                verify = self._read(addr, 16)
                if verify != payload + marker:
                    self._write(addr, old)
                    self.cancel_expected_delivery(index)
                    return "ERR", "two-slot verification failed; rolled back", ci
                self._append(
                    f"DIRECT_DELIVERY index={index} char={'Rebecca' if ci == 0 else 'Billy'} "
                    f"slots={candidate+1}-{candidate+2} item={item_id} qty={qty} room={room}\n"
                )
                return "OK", f"{'Rebecca' if ci == 0 else 'Billy'} slots {candidate+1}-{candidate+2}", ci

            candidate = None
            for si in range(6):
                it, _q = self._slot_vals(self._slot(inv, ci, si))
                if it == 0:
                    candidate = si
                    break
            if candidate is None:
                continue
            addr = root + char_off + candidate * 8
            old = self._read(addr, 8)
            if old is None:
                return "WAIT", "target slot unreadable", ci
            self.expect_ap_delivery(index, item_id, qty)
            if not self._write(addr, payload):
                self.cancel_expected_delivery(index)
                return "ERR", "inventory write failed", ci
            verify = self._read(addr, 8)
            if verify != payload:
                self._write(addr, old)
                self.cancel_expected_delivery(index)
                return "ERR", "inventory verification failed; rolled back", ci
            self._append(
                f"DIRECT_DELIVERY index={index} char={'Rebecca' if ci == 0 else 'Billy'} "
                f"slot={candidate+1} item={item_id} qty={qty} room={room}\n"
            )
            return "OK", f"{'Rebecca' if ci == 0 else 'Billy'} slot {candidate+1}", ci

        if preferred_char in (0, 1):
            target = "Rebecca" if preferred_char == 0 else "Billy"
            return "FULL", f"{target} has no safe empty slot; item remains queued", preferred_char
        return "FULL", "Rebecca has no safe empty slot; item remains queued", 0

    @staticmethod
    def _slot(raw, char_index, slot_index):
        base = char_index * 48 + slot_index * 8
        return raw[base:base+8]

    @staticmethod
    def _slot_vals(slot):
        return (
            int.from_bytes(slot[0:4], "little"),
            int.from_bytes(slot[4:8], "little"),
        )

    @classmethod
    def _fmt_inv(cls, raw):
        if raw is None or len(raw) != 96:
            return "<unavailable>"
        outs = []
        for ci, name in enumerate(("R", "B")):
            vals = []
            for si in range(6):
                it, q = cls._slot_vals(cls._slot(raw, ci, si))
                vals.append(f"{si+1}:{it}/{q}")
            outs.append(f"{name} [" + ", ".join(vals) + "]")
        return "  ".join(outs)

    @classmethod
    def _total_item(cls, raw, item_id):
        if raw is None:
            return 0
        total = 0
        for ci in range(2):
            for si in range(6):
                it, q = cls._slot_vals(cls._slot(raw, ci, si))
                if it == item_id:
                    total += q
        return total

    @classmethod
    def _slot_gains(cls, before, after, item_id):
        gains = []
        for ci, name in enumerate(("Rebecca", "Billy")):
            for si in range(6):
                bs = cls._slot(before, ci, si)
                a_s = cls._slot(after, ci, si)
                bi, bq = cls._slot_vals(bs)
                ai, aq = cls._slot_vals(a_s)
                gained = 0
                if ai == item_id:
                    if bi == ai and aq > bq:
                        gained = aq - bq
                    elif bi != ai:
                        gained = aq
                if gained > 0:
                    gains.append((gained, name, ci, si, bi, bq, ai, aq, bs, a_s))
        return gains

    @classmethod
    def _weapon_ammo_transfer_out(cls, before, after, ammo_gain):
        """Reject an item-32 gain that is simply ammo unloaded from a handgun."""
        if ammo_gain <= 0:
            return False
        lost_loaded = 0
        for ci in range(2):
            for si in range(6):
                bs = cls._slot(before, ci, si)
                a_s = cls._slot(after, ci, si)
                bi, bq = cls._slot_vals(bs)
                ai, aq = cls._slot_vals(a_s)
                if bi in (3, 4) and ai == bi and bq > aq:
                    lost_loaded += bq - aq
        return lost_loaded >= ammo_gain

    def _active_world_record_count(self, table: bytes | None) -> int:
        if not table:
            return 0
        count = 0
        for i in range(RECORD_COUNT):
            rec = self._record(table, i)
            if self._u32r(rec, 0x0C) == WORLD_ITEM_TYPE:
                count += 1
        return count

    def _startup_quarantine_tick(self, inv: bytes, table: bytes, room: int | None) -> bool:
        """Return True once first-attach state is safe to use as detector baseline.

        RE0 exposes readable room/inventory memory before the world-item manager has
        finished loading the save.  v1.8 could baseline that half-loaded state and
        then interpret the save inventory appearing as a real floor pickup.
        """
        if not self._startup_quarantine:
            return True

        now = time.monotonic()
        active = self._active_world_record_count(table)

        # An empty/nearly empty global world-item table is a strong signal that the
        # save is not actually ready yet.  A normal playable RE0 state has many
        # active world records even if the current room itself has no pickups.
        if active < 20:
            self._startup_stable_since = None
            self._startup_last_room = room
            self._startup_last_inv = bytes(inv)
            self._startup_last_table = bytes(table)
            if not self._startup_wait_logged:
                self._append(
                    f"STARTUP QUARANTINE waiting for loaded world table room={room} "
                    f"active_world={active}\n"
                )
                self._startup_wait_logged = True
            return False

        changed = (
            self._startup_last_room != room
            or self._startup_last_inv != inv
            or self._startup_last_table != table
        )
        if changed:
            self._startup_last_room = room
            self._startup_last_inv = bytes(inv)
            self._startup_last_table = bytes(table)
            self._startup_stable_since = now
            return False

        if self._startup_stable_since is None:
            self._startup_stable_since = now
            return False

        # A short stable tail after the world table is populated is enough here.
        # This is only first-attach quarantine; actual reload reconciliation keeps
        # v1.8's deliberately conservative 8s + stability logic.
        if now - self._startup_stable_since < 1.0:
            return False

        self._startup_quarantine = False
        self._append(
            f"STARTUP READY room={room} active_world={active} "
            f"stable_for={now - self._startup_stable_since:.2f}s; detector baseline armed.\n"
        )
        return True

    def _manager_table(self):
        mgr = self._u32(WORLD_MANAGER_PTR)
        if not mgr:
            return 0, None
        return mgr, self._read(mgr + TABLE_BASE_OFFSET, TABLE_SIZE)

    @staticmethod
    def _record(table, i):
        s = i * RECORD_STRIDE
        return table[s:s+RECORD_STRIDE]

    @staticmethod
    def _u32r(rec, off):
        return int.from_bytes(rec[off:off+4], "little")

    @staticmethod
    def _f32r(rec, off):
        try:
            return struct.unpack("<f", rec[off:off+4])[0]
        except Exception:
            return 0.0

    @classmethod
    def _record_fields(cls, rec):
        return {
            "link": cls._u32r(rec, 0x00),
            "room": cls._u32r(rec, 0x04),
            "type": cls._u32r(rec, 0x0C),
            "item": cls._u32r(rec, 0x10),
            "x": cls._f32r(rec, 0x28),
            "y": cls._f32r(rec, 0x2C),
            "z": cls._f32r(rec, 0x30),
            "qty": cls._u32r(rec, 0x44),
        }

    @staticmethod
    def _fmt_pos(d):
        return f"({d['x']:.3f},{d['y']:.3f},{d['z']:.3f})"

    def _specs_for_room_item(self, room: int, item_id: int):
        return [
            spec for spec in SPECS_BY_ROOM_ITEM.get((room, item_id), [])
            if spec.location_id not in self.disabled_location_ids
        ]

    def _matching_specs(self, d):
        return self._specs_for_room_item(d["room"], d["item"])

    @staticmethod
    def _is_dynamic_record_index(record_index: int) -> bool:
        # RE0's mapped static Normal-game world records end at 588.  The engine
        # reuses 589+ as its runtime/drop pool.  This is a structural identity,
        # not a heuristic: a record in this range can resemble a native pickup
        # after save/load (same room/item and even a linked record), but it is
        # still a player/runtime object and must never satisfy an AP location.
        return record_index >= DYNAMIC_RECORD_START

    def _is_dynamic_record(self, record_index: int) -> bool:
        return self._is_dynamic_record_index(record_index) or record_index in self._dynamic_records

    @staticmethod
    def _position_matches(spec, d):
        if spec.position is None:
            return True
        sx, sy, sz = spec.position
        dist2 = (d["x"] - sx) ** 2 + (d["y"] - sy) ** 2 + (d["z"] - sz) ** 2
        return dist2 <= spec.position_tolerance ** 2

    def _known_native_spec(self, record_index, d):
        if self._is_dynamic_record_index(record_index):
            return None
        for spec in self._matching_specs(d):
            if spec.scripted or spec.record_index is None:
                continue
            if spec.record_index == record_index and self._position_matches(spec, d):
                return spec
        return None

    def _identify_location(self, record_index, d):
        # Hard v1.16 invariant: runtime/drop-pool records can never be native AP
        # checks, even when their room/item/link happens to resemble one after a
        # save/load or process restart.
        if self._is_dynamic_record_index(record_index):
            return None

        # Scripted specs still participate in manager identity when RE0 happens
        # to expose a world record for them.  The scripted flag only enables the
        # inventory-only fallback when no usable native retirement is observed.
        candidates = list(self._matching_specs(d))
        if not candidates:
            return None

        # Strongest: verified record index + position.
        known = self._known_native_spec(record_index, d)
        if known is not None:
            return known

        if len(candidates) == 1:
            spec = candidates[0]
            if spec.position is None or self._position_matches(spec, d):
                return spec
            # Unique room/item remains identifiable even if we have not learned
            # this run's exact position yet. Native-vs-dynamic classification
            # is handled separately.
            return spec

        # Duplicate room/item: position is required.
        positional = [s for s in candidates if s.position is not None]
        best = None
        best_dist2 = None
        for spec in positional:
            sx, sy, sz = spec.position
            dist2 = (d["x"] - sx) ** 2 + (d["y"] - sy) ** 2 + (d["z"] - sz) ** 2
            if best_dist2 is None or dist2 < best_dist2:
                best = spec
                best_dist2 = dist2
        if best is not None and best_dist2 is not None and best_dist2 <= best.position_tolerance ** 2:
            return best
        return None

    def _class_name(self, record_index):
        if record_index in self._native_records:
            return "NATIVE"
        if self._is_dynamic_record(record_index):
            return "DYNAMIC"
        if record_index in self._pending_activations:
            return "PENDING"
        return "UNKNOWN"

    def _mark_native(self, record_index, d, spec, reason):
        if self._is_dynamic_record_index(record_index):
            # Do not let restart/baseline heuristics turn a dropped/runtime item
            # into a native AP pickup.  This was the v1.14 rec604 shell-loss bug.
            self._mark_dynamic(record_index, d, f"dynamic-range hard guard; rejected native reason={reason}")
            return
        if spec is None:
            spec = self._identify_location(record_index, d)
        if spec is None:
            self._append(
                f"NATIVE CANDIDATE UNMAPPED rec={record_index} room={d['room']} "
                f"item={d['item']} pos={self._fmt_pos(d)} reason={reason}\n"
            )
            return
        self._dynamic_records.discard(record_index)
        self._pending_activations.pop(record_index, None)
        self._native_records[record_index] = spec.location_id
        self._append(
            f"NATIVE RECORD rec={record_index} location={spec.location_id} "
            f"name={spec.name} room={d['room']} item={d['item']} qty={d['qty']} "
            f"link=0x{d['link']:08X} pos={self._fmt_pos(d)} reason={reason}\n"
        )

    def _mark_dynamic(self, record_index, d, reason):
        self._native_records.pop(record_index, None)
        self._pending_activations.pop(record_index, None)
        self._dynamic_records.add(record_index)
        self._append(
            f"DYNAMIC RECORD rec={record_index} room={d['room']} item={d['item']} "
            f"qty={d['qty']} link=0x{d['link']:08X} pos={self._fmt_pos(d)} "
            f"reason={reason}\n"
        )

    def _seed_baseline_record_classes(self, table, current_room):
        for i in range(RECORD_COUNT):
            d = self._record_fields(self._record(table, i))
            if d["type"] != WORLD_ITEM_TYPE:
                continue
            if self._is_dynamic_record_index(i):
                # Persist the structural 589+ identity across process restarts.
                # Only active runtime records are logged here; free-pool records
                # will still be protected by _is_dynamic_record_index later.
                if d["room"] != 0xFFFFFFFF:
                    self._mark_dynamic(i, d, "dynamic record range at baseline")
                continue
            if not self._matching_specs(d):
                continue
            spec = self._known_native_spec(i, d)
            if spec is not None:
                self._mark_native(i, d, spec, "verified static signature at baseline")
            elif d["link"] != 0xFFFFFFFF:
                self._mark_native(i, d, self._identify_location(i, d), "linked static at baseline")
            elif current_room is not None and d["room"] != current_room:
                self._mark_native(i, d, self._identify_location(i, d), "preloaded non-current room at baseline")
            else:
                self._append(
                    f"BASELINE UNKNOWN rec={i} room={d['room']} item={d['item']} "
                    f"qty={d['qty']} link=FFFFFFFF pos={self._fmt_pos(d)}\n"
                )

    def _log_room_records(self, table, room, label):
        if room is None:
            return
        rows = []
        for i in range(RECORD_COUNT):
            d = self._record_fields(self._record(table, i))
            if d["type"] != WORLD_ITEM_TYPE or d["room"] != room:
                continue
            specs = self._matching_specs(d)
            if not specs:
                continue
            names = " | ".join(s.name for s in specs)
            rows.append(
                f"  rec={i} class={self._class_name(i)} item={d['item']} qty={d['qty']} "
                f"link=0x{d['link']:08X} pos={self._fmt_pos(d)} candidates=[{names}]"
            )
        self._append(f"\nROOM SNAPSHOT {label} room={room}\n")
        if rows:
            self._append("\n".join(rows) + "\n")
        else:
            self._append("  <no active AP candidate records>\n")

    # ------------------------------------------------------------------
    # FACILITY MAPPER AUXILIARY LOGGING
    # These helpers are read-only.  They do not suppress or modify unknown
    # items; they simply expose every type-0x71 world-item record so later
    # areas can be catalogued without manually recording IDs/positions.
    # ------------------------------------------------------------------

    def _log_dynamic_pool_probe(self, table, label):
        """Read-only diagnostic for the repeated Facility 65->50 process crash.

        The client has twice observed RE0 terminate a few seconds after this
        transition in full-rando runs, while mapper/alpha captures have survived
        it.  Dump the active runtime/drop pool at the boundary so a future crash
        can be compared without writing anything to game memory.
        """
        rows = []
        for i in range(DYNAMIC_RECORD_START, RECORD_COUNT):
            d = self._record_fields(self._record(table, i))
            if d["type"] != WORLD_ITEM_TYPE or d["room"] == 0xFFFFFFFF:
                continue
            rows.append((i, d))
        self._append(f"CRASH_PROBE {label} active_dynamic={len(rows)}\n")
        for i, d in rows:
            self._append(
                f"CRASH_PROBE_DYNAMIC rec={i} room={d['room']} item={d['item']} "
                f"qty={d['qty']} link=0x{d['link']:08X} pos={self._fmt_pos(d)}\n"
            )


    def _map_dump_all_active(self, table, label):
        rows = []
        for i in range(RECORD_COUNT):
            d = self._record_fields(self._record(table, i))
            if d["type"] != WORLD_ITEM_TYPE:
                continue
            specs = self._matching_specs(d)
            known = " | ".join(s.name for s in specs) if specs else "UNKNOWN"
            rows.append(
                f"MAP_ACTIVE rec={i} room={d['room']} item={d['item']} qty={d['qty']} "
                f"link=0x{d['link']:08X} pos={self._fmt_pos(d)} known=[{known}]"
            )
        self._append(f"\nMAP ALL ACTIVE {label} count={len(rows)}\n")
        if rows:
            self._append("\n".join(rows) + "\n")
        else:
            self._append("  <no active world-item records>\n")

    def _map_log_room_records(self, table, room, label):
        if room is None:
            return
        rows = []
        for i in range(RECORD_COUNT):
            d = self._record_fields(self._record(table, i))
            if d["type"] != WORLD_ITEM_TYPE or d["room"] != room:
                continue
            specs = self._matching_specs(d)
            known = " | ".join(s.name for s in specs) if specs else "UNKNOWN"
            rows.append(
                f"MAP_ROOM rec={i} room={d['room']} item={d['item']} qty={d['qty']} "
                f"link=0x{d['link']:08X} pos={self._fmt_pos(d)} known=[{known}]"
            )
        self._append(f"\nMAP ROOM SNAPSHOT {label} room={room} count={len(rows)}\n")
        if rows:
            self._append("\n".join(rows) + "\n")
        else:
            self._append("  <no active world-item records in room>\n")

    def _map_process_record_changes(self, before, after):
        for i in range(RECORD_COUNT):
            old = self._record_fields(self._record(before, i))
            new = self._record_fields(self._record(after, i))
            if old == new:
                continue

            old_world = old["type"] == WORLD_ITEM_TYPE
            new_world = new["type"] == WORLD_ITEM_TYPE

            if new_world and (
                not old_world
                or old["room"] != new["room"]
                or old["item"] != new["item"]
                or old["qty"] != new["qty"]
            ):
                self._append(
                    f"MAP_ACTIVATE rec={i} room={new['room']} item={new['item']} qty={new['qty']} "
                    f"link=0x{new['link']:08X} pos={self._fmt_pos(new)} "
                    f"from_type=0x{old['type']:X} from_room={old['room']} "
                    f"from_item={old['item']} from_qty={old['qty']}\n"
                )

            if old_world and not new_world:
                self._append(
                    f"MAP_RETIRE rec={i} room={old['room']} item={old['item']} qty={old['qty']} "
                    f"link=0x{old['link']:08X} pos={self._fmt_pos(old)} "
                    f"to_type=0x{new['type']:X} to_room={new['room']} "
                    f"to_item={new['item']} to_qty={new['qty']}\n"
                )

            # Many player-dropped pickups return a record to the free pool
            # while remaining type 0x71. Log that state change explicitly.
            if old_world and new_world and (
                old["room"] != new["room"]
                or old["link"] != new["link"]
                or old["item"] != new["item"]
            ):
                self._append(
                    f"MAP_WORLD_CHANGE rec={i} "
                    f"old_room={old['room']} old_item={old['item']} old_qty={old['qty']} "
                    f"old_link=0x{old['link']:08X} old_pos={self._fmt_pos(old)} -> "
                    f"new_room={new['room']} new_item={new['item']} new_qty={new['qty']} "
                    f"new_link=0x{new['link']:08X} new_pos={self._fmt_pos(new)}\n"
                )

    def _map_inventory_slot_changes(self, before, after, room):
        if before is None or after is None:
            return
        for ci, name in enumerate(("Rebecca", "Billy")):
            for si in range(6):
                b = self._slot(before, ci, si)
                a = self._slot(after, ci, si)
                if b == a:
                    continue
                bi, bq = self._slot_vals(b)
                ai, aq = self._slot_vals(a)
                self._append(
                    f"MAP_SLOT_CHANGE room={room} char={name} slot={si+1} "
                    f"{bi}/{bq}->{ai}/{aq}\n"
                )

    def expect_ap_delivery(self, index, item_id, qty):
        now = time.monotonic()
        self._expected_deliveries = [
            e for e in self._expected_deliveries
            if now - e.when <= EXPECTED_DELIVERY_WINDOW
        ]
        self._expected_deliveries.append(
            ExpectedAPDelivery(now, int(index), int(item_id), int(qty))
        )
        self._append(
            f"EXPECT AP DELIVERY index={index} item={item_id} qty={qty} mono={now:.6f}\n"
        )

    def cancel_expected_delivery(self, index):
        self._expected_deliveries = [
            e for e in self._expected_deliveries if e.index != int(index)
        ]

    def _consume_expected_delivery(self, item_id, qty):
        now = time.monotonic()
        kept = []
        match = None
        for e in self._expected_deliveries:
            if now - e.when > EXPECTED_DELIVERY_WINDOW:
                continue
            if match is None and e.item_id == item_id and e.qty == qty:
                match = e
                continue
            kept.append(e)
        self._expected_deliveries = kept
        return match

    def _purge_recent_losses(self):
        now = time.monotonic()
        self._recent_losses = [
            x for x in self._recent_losses if now - x.when <= DROP_ACTIVATION_GRACE
        ]

    def _consume_matching_loss(self, room, item_id, qty):
        self._purge_recent_losses()
        for idx, loss in enumerate(self._recent_losses):
            if loss.room == room and loss.item_id == item_id and loss.qty == qty:
                self._recent_losses.pop(idx)
                if item_id == 55 and qty == 1:
                    self._save_candidates = [
                        c for c in self._save_candidates
                        if not (c.room == loss.room and abs(c.when - loss.when) < 0.001)
                    ]
                    self._append(
                        f"SAVE CANDIDATE CANCELLED room={room} reason=Ink Ribbon became floor drop\n"
                    )
                return loss
        return None

    def _consume_expected_rollback_loss(self, item_id, qty):
        now = time.monotonic()
        kept = []
        matched = False
        for e in self._expected_rollback_losses:
            if now - e.when > 1.0:
                continue
            if not matched and e.item_id == item_id and e.qty == qty:
                matched = True
                continue
            kept.append(e)
        self._expected_rollback_losses = kept
        return matched

    def _record_inventory_losses(self, before, after, room):
        if room is None:
            return

        # RE0 can briefly blank an entire character inventory bank during
        # partner initialization / scripted ownership transitions.  That is
        # not a player drop.  Treating it as one can poison the world-record
        # classifier when a same-item static pickup activates in the same
        # grace window (observed on the two Engine Cab handgun-ammo records).
        def _bank_nonempty(raw, ci):
            return any(
                self._slot_vals(self._slot(raw, ci, si))[0] != 0
                for si in range(6)
            )

        wiped = []
        for ci, name in enumerate(("Rebecca", "Billy")):
            if _bank_nonempty(before, ci) and not _bank_nonempty(after, ci):
                wiped.append(name)
        if wiped:
            self._append(
                f"TRANSIENT INVENTORY BANK WIPE IGNORED room={room} chars={','.join(wiped)}\n"
            )
            return

        now = time.monotonic()
        for item_id in ALL_PICKUP_ITEM_IDS:
            b = self._total_item(before, item_id)
            a = self._total_item(after, item_id)
            if a < b:
                qty = b - a
                if self._consume_expected_rollback_loss(item_id, qty):
                    self._append(
                        f"ROLLBACK LOSS IGNORED room={room} item={item_id} qty={qty}\n"
                    )
                    continue
                self._recent_losses.append(RecentLoss(now, room, item_id, qty))
                if item_id == 55 and qty == 1:
                    self._append(
                        f"INK_RIBBON_SINGLE_LOSS room={room} mono={now:.6f}; "
                        "not treated as save (v1.8 uses data0.bin writes)\n"
                    )
                self._append(
                    f"RECENT INVENTORY LOSS room={room} item={item_id} qty={qty} mono={now:.6f}\n"
                )

    def _find_pending(self, spec, record_qty):
        now = time.monotonic()
        self._pending = [
            p for p in self._pending if now - p.when <= PICKUP_MATCH_WINDOW
        ]
        # Exact source room/item/quantity first.
        for idx, p in enumerate(self._pending):
            if (
                p.room == spec.room and p.item_id == spec.item_id
                and p.gained_qty == record_qty
            ):
                return idx, p
        for idx, p in enumerate(self._pending):
            if (
                p.room == spec.room and p.item_id == spec.item_id
                and p.gained_qty == spec.vanilla_qty
            ):
                return idx, p
        for idx, p in enumerate(self._pending):
            if p.room == spec.room and p.item_id == spec.item_id:
                return idx, p
        return None, None

    def _find_pending_retirement_for_gain(self, item_id, qty):
        now = time.monotonic()
        self._pending_retirements = [
            r for r in self._pending_retirements
            if now - r.when <= PICKUP_MATCH_WINDOW
        ]
        for idx, retirement in enumerate(self._pending_retirements):
            if (
                retirement.spec.item_id == item_id
                and (retirement.record_fields["qty"] == qty or retirement.spec.vanilla_qty == qty)
            ):
                return idx, retirement
        return None, None

    def _find_two_slot_pair(self, raw, char_index, item_id):
        if raw is None:
            return None
        for si in range(5):
            it, qty = self._slot_vals(self._slot(raw, char_index, si))
            marker_it, marker_qty = self._slot_vals(self._slot(raw, char_index, si + 1))
            if it == item_id and marker_it == 180 and marker_qty == 1:
                return si, qty
        return None

    def _queue_two_slot_suppression(self, pending, record_index, spec, d, source):
        now = time.monotonic()
        self._pending_two_slot.append(
            PendingTwoSlotSuppression(
                queued_at=now,
                due_at=now + 0.35,
                expires_at=now + 2.50,
                character=pending.character,
                char_index=pending.char_index,
                item_id=spec.item_id,
                item_qty=pending.gained_qty,
                record_index=record_index,
                spec=spec,
                record_fields=dict(d),
                source=source,
            )
        )
        self._append(
            f"TWO_SLOT SUPPRESSION QUEUED rec={record_index} location={spec.location_id} "
            f"name={spec.name} character={pending.character} item={spec.item_id} "
            "delay=0.35s\n"
        )
        return True

    def _resolve_two_slot_suppressions(self):
        if not self._pending_two_slot:
            return
        now = time.monotonic()
        inv = self._inventory()
        root = self._inv_root()
        if inv is None or not root:
            return

        keep = []
        for task in self._pending_two_slot:
            if now < task.due_at:
                keep.append(task)
                continue

            pair = self._find_two_slot_pair(inv, task.char_index, task.item_id)
            if pair is None:
                if now >= task.expires_at:
                    self._append(
                        f"TWO_SLOT SUPPRESSION FAILED rec={task.record_index} "
                        f"location={task.spec.location_id} name={task.spec.name}: "
                        "item+marker pair not found before timeout; AP CHECK NOT SENT\n"
                    )
                else:
                    keep.append(task)
                continue

            si, live_qty = pair
            char_off = INV_REBECCA_OFFSET if task.char_index == 0 else INV_BILLY_OFFSET
            addr = root + char_off + si * 8
            current = self._read(addr, 16)
            if current is None:
                keep.append(task)
                continue

            it1, q1 = self._slot_vals(current[:8])
            it2, q2 = self._slot_vals(current[8:16])
            if it1 != task.item_id or it2 != 180 or q2 != 1:
                if now >= task.expires_at:
                    self._append(
                        f"TWO_SLOT SUPPRESSION FAILED rec={task.record_index} "
                        f"location={task.spec.location_id} name={task.spec.name}: "
                        f"pair changed to {it1}/{q1}, {it2}/{q2}; AP CHECK NOT SENT\n"
                    )
                else:
                    keep.append(task)
                continue

            # v0.0.13 crashed when all six inventory slots were restored during
            # Hookshot pickup. v0.0.35 only clears the confirmed two-slot pair,
            # after a short settle delay, and leaves every other slot untouched.
            if not self._write(addr, b"\x00" * 16):
                if now >= task.expires_at:
                    self._append(
                        f"TWO_SLOT SUPPRESSION WRITE FAILED rec={task.record_index} "
                        f"location={task.spec.location_id} name={task.spec.name}; "
                        "AP CHECK NOT SENT\n"
                    )
                else:
                    keep.append(task)
                continue

            self._expected_rollback_losses.append(
                ExpectedRollbackLoss(time.monotonic(), task.item_id, live_qty)
            )
            self._handled_locations.add(task.spec.location_id)
            self._remember_check_character(task.spec.location_id, task.char_index)
            self.pending_ap_checks.append(task.spec.location_id)
            self._append(
                f"TWO_SLOT_NATIVE SUPPRESSED rec={task.record_index} "
                f"location={task.spec.location_id} name={task.spec.name} "
                f"item={task.item_id} qty={live_qty} "
                f"cleared={task.character} slots={si+1}-{si+2} "
                f"pos={self._fmt_pos(task.record_fields)}\n"
                f"QUEUED_AP_CHECK location={task.spec.location_id} {task.spec.name}\n"
            )
            logger.info("AP CHECK QUEUED (two-slot): %s", task.spec.name)

        self._pending_two_slot = keep

    def _probe_same_item_combine_rollback(self, pending, record_index, spec, d, source):
        """
        Mechanics-probe-only fallback for floor Combine when the picked item was
        merged into an existing stack and the exact source slot changed before
        the normal rollback could write it back.  Deliberately restricted to the
        Cabin 201 Green Herb test location so it cannot affect normal gameplay.
        """
        return False
        # Train-only same-herb rollback experiment disabled in Lab/Factory alpha.
        if pending.before_item != 43 or pending.before_qty < 1:
            return False

        inv = self._inventory()
        root = self._inv_root()
        if inv is None or not root:
            return False

        # We only repair the very specific observed state: there must still be
        # at least the old carried stack plus the one native floor herb.
        char_total = 0
        for si in range(6):
            it, qty = self._slot_vals(self._slot(inv, pending.char_index, si))
            if it == 43:
                char_total += qty
        required = pending.before_qty + pending.gained_qty
        if char_total < required:
            self._append(
                f"COMBINE_FALLBACK REFUSED rec={record_index} location={spec.location_id} "
                f"green_total={char_total} required_at_least={required}\n"
            )
            return False

        # Prefer the original slot, then remove the gained quantity from any
        # Green Herb stack owned by the same character.
        order = [pending.slot_index] + [i for i in range(6) if i != pending.slot_index]
        remaining = pending.gained_qty
        char_off = INV_REBECCA_OFFSET if pending.char_index == 0 else INV_BILLY_OFFSET
        changes = []
        for si in order:
            if remaining <= 0:
                break
            addr = root + char_off + si * 8
            raw = self._read(addr, 8)
            if raw is None:
                continue
            it, qty = self._slot_vals(raw)
            if it != 43 or qty <= 0:
                continue
            take = min(remaining, qty)
            new_qty = qty - take
            new_raw = (b"\x00" * 8) if new_qty == 0 else (43).to_bytes(4, "little") + int(new_qty).to_bytes(4, "little")
            if not self._write(addr, new_raw):
                self._append(
                    f"COMBINE_FALLBACK WRITE FAILED rec={record_index} slot={si+1}\n"
                )
                return False
            changes.append((si + 1, qty, new_qty))
            remaining -= take

        if remaining != 0:
            self._append(
                f"COMBINE_FALLBACK INCOMPLETE rec={record_index} remaining={remaining} changes={changes}\n"
            )
            return False

        self._expected_rollback_losses.append(
            ExpectedRollbackLoss(time.monotonic(), pending.item_id, pending.gained_qty)
        )
        self._handled_locations.add(spec.location_id)
        self._remember_check_character(spec.location_id, pending.char_index)
        self.pending_ap_checks.append(spec.location_id)
        self._append(
            f"COMBINE_FALLBACK SUPPRESSED rec={record_index} location={spec.location_id} "
            f"name={spec.name} removed_native_green={pending.gained_qty} changes={changes}\n"
            f"QUEUED_AP_CHECK location={spec.location_id} {spec.name}\n"
        )
        logger.info("AP CHECK QUEUED (combine fallback): %s", spec.name)
        return True

    def _inventory_first_source(self, table, previous_table, room, item_id, gained_qty):
        """Try to identify a normal static floor source at inventory-gain time.

        The inventory gain is the trigger. World records are consulted only to
        identify the source. If multiple identical sources are live in one room,
        prefer the one that retired in the same poll; otherwise defer to the
        older retirement-confirmed path rather than guessing.
        """
        entries = {}

        def scan(snapshot_name, snapshot):
            if snapshot is None:
                return
            for i in range(RECORD_COUNT):
                d = self._record_fields(self._record(snapshot, i))
                if d["type"] != WORLD_ITEM_TYPE or d["room"] != room or d["item"] != item_id:
                    continue
                if self._is_dynamic_record(i):
                    continue
                spec = self._identify_location(i, d)
                if spec is None or spec.scripted or spec.location_id in self._handled_locations:
                    continue
                if d["qty"] != gained_qty and spec.vanilla_qty != gained_qty:
                    continue
                ent = entries.setdefault(spec.location_id, {
                    "spec": spec, "record_index": i, "CURRENT": None, "PREVIOUS": None
                })
                # Keep the record index tied to the canonical spec when known.
                if spec.record_index == i or ent["record_index"] is None:
                    ent["record_index"] = i
                ent[snapshot_name] = dict(d)

        scan("CURRENT", table)
        scan("PREVIOUS", previous_table)

        if not entries:
            return None, "no matching live/previous static source"

        vals = list(entries.values())
        if len(vals) == 1:
            ent = vals[0]
            snap = "CURRENT" if ent["CURRENT"] is not None else "PREVIOUS"
            return (snap, ent["record_index"], ent["spec"], ent[snap]), "unique source"

        # Same-room duplicate protection: if exactly one candidate existed in the
        # previous snapshot but has already disappeared from the current snapshot,
        # that is the pickup that just produced the inventory gain.
        retired_now = [e for e in vals if e["PREVIOUS"] is not None and e["CURRENT"] is None]
        if len(retired_now) == 1:
            ent = retired_now[0]
            return ("PREVIOUS_RETIRED", ent["record_index"], ent["spec"], ent["PREVIOUS"]), "same-poll retirement disambiguation"

        names = [e["spec"].name for e in vals]
        return None, f"ambiguous candidates={names}"

    def _restore_pickup(self, pending, record_index, spec, d, source="STATIC_NATIVE"):
        if spec.slots == 2:
            return self._queue_two_slot_suppression(
                pending, record_index, spec, d, source
            )

        root = self._inv_root()
        if not root:
            self._append(
                f"{source} CONFIRMED {spec.name} rec={record_index}, "
                "but inventory root unavailable; NOT RESTORED\n"
            )
            return False

        char_off = INV_REBECCA_OFFSET if pending.char_index == 0 else INV_BILLY_OFFSET
        addr = root + char_off + pending.slot_index * 8
        current = self._read(addr, 8)
        if current != pending.after_slot:
            ci, cq = self._slot_vals(current or b"\x00" * 8)
            self._append(
                f"{source} CONFIRMED {spec.name} rec={record_index}, but slot changed again "
                f"(current {ci}/{cq}); trying mechanics-probe Combine fallback.\n"
            )
            if self._probe_same_item_combine_rollback(pending, record_index, spec, d, source):
                return True
            self._append(
                f"{source} CONFIRMED {spec.name} rec={record_index}: fallback unavailable/refused; "
                "NOT SUPPRESSED.\n"
            )
            return False

        if not self._write(addr, pending.before_slot):
            self._append(
                f"{source} CONFIRMED {spec.name} rec={record_index}, "
                "inventory rollback WRITE FAILED.\n"
            )
            return False

        self._expected_rollback_losses.append(
            ExpectedRollbackLoss(time.monotonic(), pending.item_id, pending.gained_qty)
        )
        self._handled_locations.add(spec.location_id)
        self._remember_check_character(spec.location_id, pending.char_index)
        self.pending_ap_checks.append(spec.location_id)
        self._append(
            f"{source} SUPPRESSED rec={record_index} location={spec.location_id} "
            f"name={spec.name} item={d['item']} qty={d['qty']} pos={self._fmt_pos(d)}\n"
            f"RESTORED {pending.character} slot={pending.slot_index+1} "
            f"{pending.before_item}/{pending.before_qty} from "
            f"{pending.after_item}/{pending.after_qty}\n"
            f"QUEUED_AP_CHECK location={spec.location_id} {spec.name}\n"
        )
        logger.info("AP CHECK QUEUED: %s", spec.name)
        return True

    def _capture_inventory_changes(self, before, after, room, table, previous_table):
        if room is None:
            return

        # Save/load restoration repopulates the character inventories in bursts while
        # the room's vanilla floor records can already be live.  Treating those
        # restoration gains as pickups can falsely match an already-checked floor
        # source (observed with the Lecture Room Shotgun Ammo when loading in room 47),
        # leaving the one-transaction handled guard armed and allowing the real
        # re-pickup to leak vanilla loot.  The load-restore reconciler owns these
        # transitions; ordinary pickup detection resumes once it reports stable.
        if self._load_restore_pre is not None:
            self._append(
                f"LOAD_RESTORE INVENTORY TRANSITION IGNORED room={room}\n"
            )
            return

        self._record_inventory_losses(before, after, room)
        now = time.monotonic()

        # Only react to net-positive item counts. This avoids character-to-character
        # transfers and ordinary inventory rearrangement.
        interest = set(ALL_PICKUP_ITEM_IDS)
        interest.update(e.item_id for e in self._expected_deliveries)
        interest.update(r.spec.item_id for r in self._pending_retirements)

        for item_id in interest:
            b_total = self._total_item(before, item_id)
            a_total = self._total_item(after, item_id)
            net_gain = a_total - b_total
            if net_gain <= 0:
                continue

            # Handgun ammo can increase when the player unloads a handgun.
            if item_id == 32 and self._weapon_ammo_transfer_out(before, after, net_gain):
                self._append(
                    f"INVENTORY GAIN IGNORED weapon-unload room={room} item=32 qty={net_gain}\n"
                )
                continue

            expected = self._consume_expected_delivery(item_id, net_gain)
            if expected is not None:
                self._append(
                    f"AP_DELIVERY_GAIN IGNORED index={expected.index} room={room} "
                    f"item={item_id} qty={net_gain}\n"
                )
                continue

            gains = self._slot_gains(before, after, item_id)
            if not gains:
                continue
            chosen = next((g for g in gains if g[0] == net_gain), gains[0])
            gained, name, ci, si, bi, bq, ai, aq, bs, a_s = chosen

            # Preserve the old retirement-first ordering rescue for rare cases
            # where the world record retired on a previous poll before inventory.
            ridx, retirement = self._find_pending_retirement_for_gain(item_id, gained)
            if retirement is not None:
                self._pending_retirements.pop(ridx)
                p = PendingPickup(
                    now, name, ci, si, item_id, gained,
                    bi, bq, ai, aq, bs, a_s, retirement.spec.room
                )
                self._append(
                    f"\nRETIREMENT-FIRST MATCH rec={retirement.record_index} "
                    f"name={retirement.spec.name} gain_room={room} item={item_id} qty={gained}\n"
                )
                self._restore_pickup(
                    p, retirement.record_index, retirement.spec,
                    retirement.record_fields, "RETIREMENT_FIRST"
                )
                continue

            room_specs = self._specs_for_room_item(room, item_id)
            if not room_specs:
                continue

            p = PendingPickup(
                now, name, ci, si, item_id, gained,
                bi, bq, ai, aq, bs, a_s, room
            )

            normal_specs = [sp for sp in room_specs if not sp.scripted]
            if normal_specs:
                source, reason = self._inventory_first_source(
                    table, previous_table, room, item_id, gained
                )
                if source is not None:
                    snapshot_name, record_index, spec, record_fields = source
                    self._append(
                        f"\nINVENTORY_FIRST MATCH snapshot={snapshot_name} "
                        f"rec={record_index} location={spec.location_id} name={spec.name} "
                        f"room={room} item={item_id} qty={gained} slot={si+1}\n"
                    )
                    self._restore_pickup(
                        p, record_index, spec, record_fields, "INVENTORY_FIRST"
                    )
                    continue

                # Do not guess. Keep the gain pending so the old record-retirement
                # confirmation can identify duplicates or unusual static pickups.
                self._pending.append(p)
                self._append(
                    f"\nINVENTORY_FIRST DEFERRED {name} slot={si+1} room={room} "
                    f"item={item_id} gained={gained} reason={reason}; "
                    "waiting for retirement confirmation\n"
                )
                continue

            # Scripted sources (Leech Capsule/Input Coil cabinets) have no useful
            # static floor-record retirement, so retain the proven delayed
            # inventory-only fallback.
            self._pending.append(p)
            self._append(
                f"\nSCRIPTED INVENTORY PENDING {name} slot={si+1} room={room} "
                f"item={item_id} gained={gained} mono={now:.6f}\n"
            )

    def _native_retirement(self, record_index, old):
        if self._is_dynamic_record_index(record_index):
            return None
        spec = None
        loc = self._native_records.get(record_index)
        if loc is not None:
            spec = SPEC_BY_LOCATION.get(loc)
        if spec is None:
            spec = self._known_native_spec(record_index, old)
        if spec is None and old["link"] != 0xFFFFFFFF:
            spec = self._identify_location(record_index, old)
        return spec

    def _process_native_retirement(self, record_index, old, spec):
        handled = spec.location_id in self._handled_locations
        idx, p = self._find_pending(spec, old["qty"])
        self._append(
            f"STATIC_RECORD RETIRED rec={record_index} location={spec.location_id} "
            f"name={spec.name} qty={old['qty']} link=0x{old['link']:08X} "
            f"pos={self._fmt_pos(old)} pending={'yes' if p else 'no'} "
            f"already_inventory_handled={'yes' if handled else 'no'}\n"
        )
        self._native_records.pop(record_index, None)
        self._pending_activations.pop(record_index, None)

        if handled:
            # The location guard only spans this one native pickup transaction.
            # Discard it once the floor record retires so an older save can
            # respawn and be suppressed again without awarding vanilla loot.
            self._handled_locations.discard(spec.location_id)
            self._append(
                f"WORLD_RETIRE_IGNORED rec={record_index} location={spec.location_id} "
                "reason=inventory-first transaction already complete\n"
            )
            return

        if p is not None:
            self._pending.pop(idx)
            self._restore_pickup(p, record_index, spec, old, "RETIREMENT_CONFIRMED")
        else:
            # No ordinary inventory pickup matched. Keep the existing floor-action
            # fallback so direct Use/Combine still sends the AP check and cannot
            # delete progression from a multiworld. Vanilla side effects may remain.
            self._pending_retirements.append(
                PendingRetirement(
                    time.monotonic(), record_index, spec, dict(old),
                    self._last_inv, self._inventory()
                )
            )
            self._append(
                f"PENDING RETIREMENT rec={record_index} location={spec.location_id} "
                f"name={spec.name}\n"
            )
            live_inv = self._inventory()
            self._append(
                f"FLOOR_ACTION_DIAGNOSTIC {spec.name} retired without an immediately "
                f"matched gain; live_inv={self._fmt_inv(live_inv)}\n"
            )

    def _process_record_changes(self, before, after, current_room):
        now = time.monotonic()
        for i in range(RECORD_COUNT):
            old = self._record_fields(self._record(before, i))
            new = self._record_fields(self._record(after, i))
            if old == new:
                continue

            old_specs = self._matching_specs(old)
            new_specs = self._matching_specs(new)

            became_active = (
                new["type"] == WORLD_ITEM_TYPE
                and bool(new_specs)
                and (
                    old["type"] != WORLD_ITEM_TYPE
                    or old["room"] != new["room"]
                    or old["item"] != new["item"]
                )
            )
            if became_active:
                # v0.0.37: a matching inventory loss is the strongest evidence
                # that this activation is a player drop.  Some dropped key
                # records (observed with Dining Car Key item 103) can reactivate
                # with an ordinary-looking linked record, so link/static
                # heuristics must NOT outrank a fresh drop-loss match.
                spec = self._known_native_spec(i, new)

                # An exact record-index + position signature is stronger than a
                # recent inventory-loss heuristic. A transient partner-bank wipe
                # can otherwise make a genuine static pickup look like a player
                # drop. Real dropped items normally reactivate at a different
                # position/record signature and still use the loss path below.
                if spec is not None:
                    self._mark_native(i, new, spec, "verified static signature")
                else:
                    loss = self._consume_matching_loss(new["room"], new["item"], new["qty"])
                    if loss is not None:
                        self._mark_dynamic(i, new, "matched inventory drop loss (priority)")
                    elif self._is_dynamic_record(i):
                        self._append(
                            f"DYNAMIC RECORD REACTIVATED rec={i} room={new['room']} item={new['item']} "
                            f"pos={self._fmt_pos(new)}\n"
                        )
                    elif new["link"] != 0xFFFFFFFF:
                        self._mark_native(i, new, self._identify_location(i, new), "linked static activation")
                    elif current_room is not None and new["room"] != current_room:
                        self._mark_native(i, new, self._identify_location(i, new), "room preload activation")
                    else:
                        self._pending_activations[i] = PendingActivation(
                            now, i, dict(new), self._identify_location(i, new)
                        )
                        self._append(
                            f"ACTIVATION PENDING rec={i} room={new['room']} item={new['item']} "
                            f"qty={new['qty']} link=FFFFFFFF pos={self._fmt_pos(new)}\n"
                        )

            # Native/static pickup: type 0x71 -> type 0.
            if old["type"] == WORLD_ITEM_TYPE and new["type"] == 0 and old_specs:
                if self._is_dynamic_record(i):
                    # A player-dropped item is being picked back up.  Keep the
                    # inventory gain and remove its pending pickup so a scripted
                    # source with the same room/item cannot suppress it later.
                    dummy_spec = old_specs[0]
                    idx, p = self._find_pending(dummy_spec, old["qty"])
                    self._append(
                        f"DYNAMIC TYPE0 PASS rec={i} room={old['room']} item={old['item']} "
                        f"qty={old['qty']} pos={self._fmt_pos(old)} "
                        f"pending={'yes' if p else 'no'}\n"
                    )
                    if p is not None:
                        self._pending.pop(idx)
                        self._append(
                            f"DROPPED PICKUP KEPT: {p.character} slot={p.slot_index+1} "
                            f"{p.before_item}/{p.before_qty}->{p.after_item}/{p.after_qty}\n"
                        )
                    self._dynamic_records.discard(i)
                    self._pending_activations.pop(i, None)
                    continue

                spec = self._native_retirement(i, old)
                if spec is not None:
                    self._process_native_retirement(i, old, spec)
                else:
                    self._append(
                        f"AMBIGUOUS RETIREMENT rec={i} room={old['room']} item={old['item']} "
                        f"qty={old['qty']} link=0x{old['link']:08X} "
                        f"pos={self._fmt_pos(old)}; NOT SUPPRESSED\n"
                    )
                continue

            # Player-dropped pickup returns the record to the free pool.
            if (
                old["type"] == WORLD_ITEM_TYPE
                and old["link"] == 0xFFFFFFFF
                and old_specs
                and new["type"] == WORLD_ITEM_TYPE
                and new["item"] == old["item"]
                and new["room"] == 0xFFFFFFFF
                and new["link"] != 0xFFFFFFFF
            ):
                native_spec = None
                if i in self._native_records:
                    native_spec = SPEC_BY_LOCATION.get(self._native_records[i])
                if native_spec is None:
                    native_spec = self._known_native_spec(i, old)

                if native_spec is not None:
                    self._native_records.pop(i, None)
                    self._pending_activations.pop(i, None)
                    self._append(
                        f"NATIVE RECORD UNLOAD rec={i} location={native_spec.location_id} "
                        f"name={native_spec.name} room={old['room']} item={old['item']} "
                        f"free_link=0x{new['link']:08X}\n"
                    )
                    continue

                self._dynamic_records.add(i)
                self._native_records.pop(i, None)
                self._pending_activations.pop(i, None)

                # A dropped pickup/unload must cancel a matching pending inventory gain
                # when there actually was a gain in the pickup window.
                # regardless of which same-room location spec it resembles.
                dummy_spec = old_specs[0]
                idx, p = self._find_pending(dummy_spec, old["qty"])
                self._append(
                    f"DROPPED_DYNAMIC PASS rec={i} room={old['room']} item={old['item']} "
                    f"qty={old['qty']} free_link=0x{new['link']:08X} "
                    f"pending={'yes' if p else 'no'}\n"
                )
                if p is not None:
                    self._pending.pop(idx)
                    self._append(
                        f"DROPPED PICKUP KEPT: {p.character} slot={p.slot_index+1} "
                        f"{p.before_item}/{p.before_qty}->{p.after_item}/{p.after_qty}\n"
                    )
                    logger.info(
                        "DROPPED_DYNAMIC PASS: player-dropped item %d in room %d left untouched.",
                        old["item"], old["room"],
                    )

    def _resolve_pending_activations(self):
        now = time.monotonic()
        for i, activation in list(self._pending_activations.items()):
            d = activation.record_fields
            loss = self._consume_matching_loss(d["room"], d["item"], d["qty"])
            if loss is not None:
                self._mark_dynamic(i, d, "delayed inventory drop-loss match")
                continue
            if now - activation.when >= DROP_ACTIVATION_GRACE:
                self._mark_native(i, d, activation.spec, "no inventory drop loss during activation grace")

    def _resolve_save_candidates(self):
        # v1.8: disabled. One Ink Ribbon disappearing is not proof of a save.
        if self._save_candidates:
            self._save_candidates.clear()

    def _resolve_stale_pickups(self):
        now = time.monotonic()

        # Conservative scripted fallback after enough time for a dropped-object
        # free-list transition to have appeared.
        for p in list(self._pending):
            age = now - p.when
            if age < SCRIPTED_FALLBACK_DELAY:
                continue
            scripted = [
                s for s in self._specs_for_room_item(p.room, p.item_id)
                if s.scripted
            ]
            if len(scripted) == 1:
                spec = scripted[0]

                if spec.location_id in self._handled_locations:
                    self._pending.remove(p)
                    self._append(
                        f"SCRIPTED ALREADY_HANDLED PASS location={spec.location_id} "
                        f"name={spec.name} room={p.room} item={p.item_id} "
                        f"qty={p.gained_qty}; pickup kept\n"
                    )
                    continue

                d = {
                    "item": p.item_id,
                    "qty": p.gained_qty,
                    "x": 0.0, "y": 0.0, "z": 0.0,
                    "room": p.room, "link": 0xFFFFFFFF, "type": 0,
                }
                self._pending.remove(p)
                self._append(
                    f"SCRIPTED PICKUP CONFIRMED after {age:.3f}s: {spec.name} "
                    f"room={p.room} item={p.item_id} qty={p.gained_qty}\n"
                )
                self._restore_pickup(p, -1, spec, d, "SCRIPTED_NATIVE")
                continue

        stale = [p for p in self._pending if now - p.when > PICKUP_MATCH_WINDOW]
        for p in stale:
            self._append(
                f"PENDING EXPIRED {p.character} room={p.room} "
                f"item={p.item_id} gained={p.gained_qty}\n"
            )
        self._pending = [
            p for p in self._pending if now - p.when <= PICKUP_MATCH_WINDOW
        ]

        # Mechanics-probe v1.3: Pick Up, Use and Combine do not always share
        # RE0's normal inventory-gain path.  For the two dedicated Green Herb
        # floor-action test locations, a retired native world record is enough
        # to prove the interaction happened.  Give normal Pick Up a short grace
        # period to match its inventory gain first; if no gain arrives, treat it
        # as Use/Combine, restore any menu-combine inventory mutation, and send
        # the AP check anyway.  A floor Use can heal before we see the retirement;
        # health rollback is intentionally out of scope for this probe.
        # Alpha safety net: if a known one-slot static AP record retires without
        # producing a normal inventory gain (floor Use/Combine), count the check
        # after the same short grace period. Vanilla side effects may still occur.
        FLOOR_ACTION_TEST_LOCATIONS = {
            s.location_id for s in PICKUP_LOCATIONS if not s.scripted and s.slots == 1
        }
        floor_ready = [
            r for r in self._pending_retirements
            if r.spec.location_id in FLOOR_ACTION_TEST_LOCATIONS and now - r.when > 0.25
        ]
        for r in floor_ready:
            if r.spec.location_id in self._handled_locations:
                self._pending_retirements.remove(r)
                continue

            restored = False
            changed_slots = []
            if r.inv_before is not None and r.inv_after is not None and r.inv_before != r.inv_after:
                root = self._inv_root()
                live = self._inventory()
                if root and live is not None:
                    # Combine occurs while controlling Rebecca in the Train test,
                    # but compare both characters and only rewind slots whose live
                    # value still equals the captured post-action value.  That keeps
                    # the rollback narrow and avoids overwriting a later unrelated
                    # inventory change.
                    for ci, char_off, cname in (
                        (0, INV_REBECCA_OFFSET, 'Rebecca'),
                        (1, INV_BILLY_OFFSET, 'Billy'),
                    ):
                        for si in range(6):
                            bslot = self._slot(r.inv_before, ci, si)
                            aslot = self._slot(r.inv_after, ci, si)
                            if bslot == aslot:
                                continue
                            lslot = self._slot(live, ci, si)
                            if lslot != aslot:
                                continue
                            addr = root + char_off + si * 8
                            if self._write(addr, bslot):
                                bi, bq = self._slot_vals(bslot)
                                ai, aq = self._slot_vals(aslot)
                                changed_slots.append((cname, si + 1, ai, aq, bi, bq))
                                restored = True

            self._handled_locations.add(r.spec.location_id)
            if changed_slots:
                self._remember_check_character(r.spec.location_id, 0 if changed_slots[0][0] == "Rebecca" else 1)
            self.pending_ap_checks.append(r.spec.location_id)
            self._append(
                f"FLOOR_ACTION FALLBACK rec={r.record_index} location={r.spec.location_id} "
                f"name={r.spec.name} inventory_restored={restored} changes={changed_slots}\n"
                f"QUEUED_AP_CHECK location={r.spec.location_id} {r.spec.name}\n"
            )
            if not restored and r.inv_before == r.inv_after:
                self._append(
                    f"FLOOR_ACTION NOTE location={r.spec.location_id}: no inventory mutation; "
                    "likely floor Use. Any healing side effect is left vanilla in this probe.\n"
                )
            self._pending_retirements.remove(r)

        stale_ret = [
            r for r in self._pending_retirements
            if now - r.when > PICKUP_MATCH_WINDOW
        ]
        for r in stale_ret:
            self._append(
                f"PENDING RETIREMENT EXPIRED rec={r.record_index} "
                f"location={r.spec.location_id} name={r.spec.name}\n"
            )
        self._pending_retirements = [
            r for r in self._pending_retirements
            if now - r.when <= PICKUP_MATCH_WINDOW
        ]

        self._expected_deliveries = [
            e for e in self._expected_deliveries
            if now - e.when <= EXPECTED_DELIVERY_WINDOW
        ]
        self._expected_rollback_losses = [
            e for e in self._expected_rollback_losses
            if now - e.when <= 1.0
        ]
        self._resolve_save_candidates()
        self._purge_recent_losses()

    def _static_capture(self):
        if self._static_done_pid == self.pid:
            return True
        sig = self._read(INVENTORY_SWITCH, 0x30)
        if not sig or UNPACKED_SIGNATURE not in sig:
            return False
        self._append(
            "\n============================================================\n"
            "RE0 AP SPLIT-CLIENT v1.20-strict-character\n"
            f"captured={time.strftime('%Y-%m-%d %H:%M:%S')} pid={self.pid}\n"
            "NO DEBUGGER / NO BREAKPOINTS / NO CODE PATCHES\n"
            f"record-classified AP locations={len(PICKUP_LOCATIONS)}\n"
            "DEFAULT NORMAL: 187 mapped checks; inventory-first pickup suppression; scripted fallback; direct Python AP delivery; serialized ordered save/load replay; first-attach startup quarantine; data0.bin save watermark; room-commit load gate; room165/next165/menu21 victory.\n"
            "============================================================\n"
        )
        self._static_done_pid = self.pid
        logger.info(
            "RE0 FULL NORMAL detector READY: %d AP pickups; log=%s",
            len(PICKUP_LOCATIONS), self.log_path
        )
        return True

    async def loop(self, exit_event):
        if sys.platform != "win32":
            logger.warning("RE0 full Normal memory client is Windows-only.")
            return

        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.write(
                    "RE0 AP SPLIT-CLIENT v1.20-strict-character - waiting for active gameplay...\n"
                    f"log={self.log_path}\n"
                )
        except Exception:
            pass

        while not exit_event.is_set():
            try:
                if not self._connect():
                    await asyncio.sleep(0.20)
                    continue
                if not self._static_capture():
                    await asyncio.sleep(0.10)
                    continue

                room = self._room()
                inv = self._inventory()
                mgr, table = self._manager_table()
                if inv is None or table is None:
                    self._read_failures += 1
                    if self._read_failures == 20:
                        self._append(
                            f"\nPROCESS READS LOST @ {time.strftime('%H:%M:%S')} "
                            f"pid={self.pid}; disconnecting probe handle.\n"
                        )
                        self._arm_load_restore()
                        self._disconnect()
                    await asyncio.sleep(0.02)
                    continue
                self._read_failures = 0
                self._goal_tick(room)
                self._load_restore_tick(inv, table, room)

                if self._last_table is None:
                    # On the very first attachment there is no pre-load snapshot to
                    # reconcile against.  Quarantine the boot/load transition until
                    # the global item table and inventory have genuinely settled.
                    # If this is a reconnect with an armed restore snapshot, the
                    # load reconciler already owns the transition instead.
                    if self._load_restore_pre is None and not self._startup_quarantine_tick(inv, table, room):
                        await asyncio.sleep(0.02)
                        continue
                    self._last_table = table
                    self._last_inv = inv
                    self._last_room = room
                    self._seed_baseline_record_classes(table, room)
                    self._append(f"\nBASELINE room={room}\nINV {self._fmt_inv(inv)}\n")
                    self._log_room_records(table, room, "baseline")
                    self._map_dump_all_active(table, "baseline")
                    self._map_log_room_records(table, room, "baseline")
                    await asyncio.sleep(0.005)
                    continue

                if inv != self._last_inv:
                    self._map_inventory_slot_changes(self._last_inv, inv, room)
                    self._capture_inventory_changes(self._last_inv, inv, room, table, self._last_table)
                    self._append(
                        f"INVENTORY CHANGE room={room}\n"
                        f"BEFORE {self._fmt_inv(self._last_inv)}\n"
                        f"AFTER  {self._fmt_inv(inv)}\n"
                    )

                if table != self._last_table:
                    self._map_process_record_changes(self._last_table, table)
                    self._process_record_changes(self._last_table, table, room)

                self._resolve_pending_activations()

                if room != self._last_room:
                    self._append(
                        f"\nROOM CHANGE {self._last_room}->{room} "
                        f"@ {time.strftime('%H:%M:%S')}\n"
                    )
                    self._log_room_records(table, room, "room change")
                    self._map_log_room_records(table, room, "room change")
                    if room in (65, 50):
                        self._log_dynamic_pool_probe(table, f"room_change {self._last_room}->{room}")

                self._resolve_stale_pickups()
                self._resolve_two_slot_suppressions()

                # IMPORTANT: retain the exact snapshots processed this iteration.
                # v0.0.33 re-read inventory/room here and could swallow a fast
                # post-retirement gain (observed on the Freight Car Gold Ring).
                self._last_table = table
                self._last_inv = inv
                self._last_room = room
                await asyncio.sleep(0.005)

            except Exception as exc:
                logger.exception("Lab + Factory detector error: %s", exc)
                self._append(f"\nDETECTOR ERROR: {exc!r}\n")
                self._disconnect()
                await asyncio.sleep(0.20)

        self._disconnect()
# ---------------------------------------------------------------------------

@dataclass
class PendingDelivery:
    index: int
    ap_item_id: int
    re0_item_id: int
    count: int
    slots: int
    display_name: str
    source_location: int = 0
    source_player: int = 0
    preferred_char: int | None = None
    is_restore: bool = False


class RE0CommandProcessor(ClientCommandProcessor):
    def _cmd_delivery(self) -> bool:
        """Show direct-Python item delivery and pending queue status."""
        ctx: RE0Context = self.ctx  # type: ignore[assignment]
        self.output(
            f"RE0 direct delivery: {ctx.delivery_status}; pending deliveries: {len(ctx.pending)}; "
            f"reported checks: {len(ctx.reported_checks)}; delivered AP indexes: {len(ctx.delivered_indices)}"
        )
        return True

    def _cmd_bridge(self) -> bool:
        """Compatibility alias for /delivery. This build does not use an ASI bridge."""
        return self._cmd_delivery()

    def _cmd_probe(self) -> bool:
        """Show the full-game detector log path."""
        ctx: RE0Context = self.ctx  # type: ignore[assignment]
        self.output(f"RE0 full Normal log: {ctx.memory_probe.log_path}")
        return True

    def _cmd_journal(self) -> bool:
        """Show save/load journal status."""
        ctx: RE0Context = self.ctx  # type: ignore[assignment]
        self.output(
            f"RE0 journal: {ctx.journal_path or 'not connected to a seed yet'}; "
            f"known delivered indexes: {len(ctx.delivered_indices)}"
        )
        return True


class RE0Context(CommonContext):
    command_processor = RE0CommandProcessor
    game = "Resident Evil 0"
    items_handling = 0b111

    def __init__(self, server_address: Optional[str], password: Optional[str]):
        super().__init__(server_address, password)
        self.pending: list[PendingDelivery] = []
        self.queued_indices: set[int] = set()
        self.delivered_indices: set[int] = set()
        self.delivery_history: dict[int, dict] = {}
        self.reported_checks: set[int] = set()
        self.delivery_status = "waiting for gameplay"
        self.delivery_task: Optional[asyncio.Task] = None
        self.location_task: Optional[asyncio.Task] = None
        self.restore_task: Optional[asyncio.Task] = None
        self.goal_task: Optional[asyncio.Task] = None
        self.memory_probe = RE0MemoryProbe()
        self.probe_task: Optional[asyncio.Task] = None
        self.journal_path: str | None = None
        self._journal_identity: tuple[str, int, str] | None = None
        self._restore_counter = 0
        self.saved_delivered_indices: set[int] = set()
        self.save_watermark_known = False
        self._persistent_restore_inv: bytes | None = None
        self._persistent_restore_table: bytes | None = None
        # Frozen set of *real AP delivery indexes that had actually reached RE0*
        # at the instant gameplay disappeared.  This is deliberately separate
        # from self.delivered_indices, which may continue to change later when
        # queued server items are finally inserted.  v1.6 used the live set during
        # reconciliation, allowing a queued item delivered just after load to be
        # mistaken for a consumed post-save item and replayed a second time.
        self._restore_delivered_indices: set[int] = set()
        # While True, ordinary AP deliveries are frozen.  The restore loop gets
        # first ownership of the stable post-load snapshot and builds one restore
        # plan before the normal queue is allowed to move again.
        self._restore_reconciling = False
        self._restore_batch_remaining = 0
        self.save_task: Optional[asyncio.Task] = None
        self._save_file_path: str | None = None
        self._save_file_signature: tuple[int, int] | None = None
        self._save_file_logged_missing = False
        self.memory_probe.load_restore_snapshot_callback = self._persist_load_restore_snapshot
        # AP 0.6.7 does not expose CommonContext.server_seed_name.
        # Capture RoomInfo.seed_name ourselves so the save/load journal can still
        # be safely namespaced per seed on both 0.6.7 and newer clients.
        self._ap_server_seed_name: str | None = None
        self.active_location_count = len(LOCATION_DEFS)

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    @staticmethod
    def _safe_filename(text: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "unknown")
        return text[:100] or "unknown"

    def _journal_file_for_connection(self, args: dict) -> str:
        seed = str(self._ap_server_seed_name or getattr(self, "server_seed_name", None) or self.seed_name or "unknown-seed")
        slot = int(args.get("slot", self.slot or 0))
        auth = str(self.auth or "Torde")
        name = (
            f"RE0_AP_full_journal_{self._safe_filename(seed)}_"
            f"slot{slot}_{self._safe_filename(auth)}.json"
        )
        return os.path.join(tempfile.gettempdir(), name)

    def _load_journal(self, args: dict) -> None:
        seed = str(self._ap_server_seed_name or getattr(self, "server_seed_name", None) or self.seed_name or "unknown-seed")
        slot = int(args.get("slot", self.slot or 0))
        auth = str(self.auth or "Torde")
        identity = (seed, slot, auth)
        path = self._journal_file_for_connection(args)
        self.journal_path = path
        if self._journal_identity == identity:
            return

        # A server/seed/slot switch is a new run. Do not carry delivery indexes
        # across seeds even when the player name is the same.
        self._journal_identity = identity
        self.delivered_indices.clear()
        self.delivery_history.clear()
        self.pending.clear()
        self.queued_indices.clear()
        self.saved_delivered_indices.clear()
        self.save_watermark_known = False
        self._persistent_restore_inv = None
        self._persistent_restore_table = None
        self._restore_delivered_indices.clear()
        self._restore_reconciling = False
        self._restore_batch_remaining = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if (
                payload.get("seed") == seed
                and int(payload.get("slot", -1)) == slot
                and payload.get("auth") == auth
            ):
                self.delivered_indices.update(int(x) for x in payload.get("delivered_indices", []))
                raw_history = payload.get("delivery_history", {})
                for key, value in raw_history.items():
                    try:
                        self.delivery_history[int(key)] = dict(value)
                    except Exception:
                        continue
                self.save_watermark_known = bool(payload.get("save_watermark_known", False))
                self.saved_delivered_indices.update(
                    int(x) for x in payload.get("saved_delivered_indices", [])
                )
                snap = payload.get("restore_snapshot") or {}
                if isinstance(snap, dict) and snap.get("inv_b64"):
                    try:
                        inv = base64.b64decode(str(snap.get("inv_b64")))
                        table_b64 = snap.get("table_b64")
                        table = base64.b64decode(str(table_b64)) if table_b64 else None
                        if len(inv) == 96 and (table is None or len(table) == TABLE_SIZE):
                            self._persistent_restore_inv = inv
                            self._persistent_restore_table = table
                            frozen = snap.get("delivered_indices")
                            if isinstance(frozen, list):
                                self._restore_delivered_indices = {int(x) for x in frozen}
                            else:
                                # Backward-compatible v1.6 journal fallback.  A fresh
                                # v1.8 snapshot always stores the exact frozen set.
                                self._restore_delivered_indices = set(self.delivered_indices)
                            self._restore_reconciling = True
                            self.memory_probe.seed_load_restore_snapshot(inv, table)
                            logger.info(
                                "Loaded pending RE0 crash/save restore snapshot from journal (%d frozen deliveries).",
                                len(self._restore_delivered_indices),
                            )
                    except Exception as exc:
                        logger.warning("Could not decode RE0 restore snapshot: %s", exc)
                logger.info(
                    "Loaded RE0 journal for %s: %d previously delivered AP indexes.",
                    seed, len(self.delivered_indices),
                )
        except FileNotFoundError:
            logger.info("Starting new RE0 journal for seed %s.", seed)
        except Exception as exc:
            logger.warning("Could not load RE0 journal %s: %s", path, exc)

    def _save_journal(self) -> None:
        if not self.journal_path or not self._journal_identity:
            return
        seed, slot, auth = self._journal_identity
        snapshot = None
        if self._persistent_restore_inv is not None:
            snapshot = {
                "inv_b64": base64.b64encode(self._persistent_restore_inv).decode("ascii"),
                "table_b64": (
                    base64.b64encode(self._persistent_restore_table).decode("ascii")
                    if self._persistent_restore_table is not None else None
                ),
                "delivered_indices": sorted(self._restore_delivered_indices),
            }
        payload = {
            "version": 3,
            "seed": seed,
            "slot": slot,
            "auth": auth,
            "delivered_indices": sorted(self.delivered_indices),
            "delivery_history": {str(k): v for k, v in sorted(self.delivery_history.items())},
            "save_watermark_known": bool(self.save_watermark_known),
            "saved_delivered_indices": sorted(self.saved_delivered_indices),
            "restore_snapshot": snapshot,
        }
        temp_path = self.journal_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(temp_path, self.journal_path)
        except Exception as exc:
            logger.warning("Could not save RE0 journal %s: %s", self.journal_path, exc)

    def _persist_load_restore_snapshot(self, inv: bytes | None, table: bytes | None) -> None:
        if inv is None or len(inv) != 96:
            return

        # If the player reloads/crashes again while an earlier synthetic restore is
        # still waiting for space, that stale synthetic work must not survive into
        # the new comparison.  The new pre-loss snapshot already reflects whatever
        # actually made it into RE0, so the new reconciliation will recreate only
        # the still-missing amount.  Real AP deliveries remain pending untouched.
        stale_restore = sum(1 for d in self.pending if d.is_restore)
        if stale_restore:
            self.pending = [d for d in self.pending if not d.is_restore]
            self.memory_probe._append(
                f"LOAD_RESTORE PURGED stale_synthetic={stale_restore} before new snapshot.\n"
            )

        self._persistent_restore_inv = bytes(inv)
        self._persistent_restore_table = bytes(table) if table is not None else None
        self._restore_delivered_indices = set(self.delivered_indices)
        self._restore_reconciling = True
        self._restore_batch_remaining = 0
        self._save_journal()
        real_pending = [d.index for d in self.pending if not d.is_restore]
        self.memory_probe._append(
            f"LOAD_RESTORE SNAPSHOT persisted; frozen_delivered={len(self._restore_delivered_indices)} "
            f"normal_pending={real_pending}.\n"
        )

    def _clear_persistent_restore_snapshot(self) -> None:
        had_snapshot = self._persistent_restore_inv is not None or self._persistent_restore_table is not None
        self._persistent_restore_inv = None
        self._persistent_restore_table = None
        self._restore_delivered_indices.clear()
        self._restore_reconciling = False
        self._restore_batch_remaining = 0
        self._save_journal()
        if had_snapshot:
            self.memory_probe._append("LOAD_RESTORE SNAPSHOT cleared after successful reconciliation.\n")

    @staticmethod
    def _save_file_sig(path: str) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    def _discover_re0_save_file(self) -> str | None:
        """Find Steam RE0 data0.bin without knowing the Steam account id."""
        roots: list[str] = []
        if sys.platform == "win32":
            try:
                import winreg
                probes = (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
                )
                for hive, key, value in probes:
                    try:
                        with winreg.OpenKey(hive, key) as h:
                            p, _ = winreg.QueryValueEx(h, value)
                        if p:
                            roots.append(os.path.normpath(str(p)))
                    except OSError:
                        pass
            except Exception:
                pass
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env_name)
            if base:
                roots.append(os.path.join(base, "Steam"))

        candidates: list[str] = []
        seen: set[str] = set()
        for root in roots:
            root = os.path.normcase(os.path.abspath(root))
            if root in seen:
                continue
            seen.add(root)
            userdata = os.path.join(root, "userdata")
            try:
                users = os.listdir(userdata)
            except OSError:
                continue
            for uid in users:
                p = os.path.join(userdata, uid, "339340", "remote", "data0.bin")
                if os.path.isfile(p):
                    candidates.append(p)
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda p: os.stat(p).st_mtime_ns)
        except OSError:
            return candidates[0]

    async def save_watermark_loop(self):
        """Use the actual RE0 save-file write as the save authority."""
        while not self.exit_event.is_set():
            if not self._save_file_path or not os.path.isfile(self._save_file_path):
                self._save_file_path = self._discover_re0_save_file()
                self._save_file_signature = None
                if self._save_file_path:
                    self._save_file_signature = self._save_file_sig(self._save_file_path)
                    self._save_file_logged_missing = False
                    self.memory_probe._append(
                        f"SAVEFILE WATCH path={self._save_file_path} sig={self._save_file_signature}\n"
                    )
                elif not self._save_file_logged_missing:
                    self._save_file_logged_missing = True
                    self.memory_probe._append(
                        "SAVEFILE WATCH data0.bin not found yet; save watermark disabled until discovered.\n"
                    )
                await asyncio.sleep(1.0)
                continue

            sig = self._save_file_sig(self._save_file_path)
            if sig is None:
                self._save_file_path = None
                await asyncio.sleep(0.5)
                continue
            if self._save_file_signature is None:
                self._save_file_signature = sig
                await asyncio.sleep(0.20)
                continue
            if sig == self._save_file_signature:
                await asyncio.sleep(0.20)
                continue

            # Debounce multi-write save updates.
            await asyncio.sleep(0.75)
            final_sig = self._save_file_sig(self._save_file_path) or sig
            old_sig = self._save_file_signature
            self._save_file_signature = final_sig

            active = bool(self.memory_probe.handle and self.memory_probe._last_room is not None)
            if not active:
                self.memory_probe._append(
                    f"SAVEFILE WRITE IGNORED old={old_sig} new={final_sig} reason=no_active_gameplay\n"
                )
                continue
            if self._restore_reconciling or self._restore_batch_remaining > 0 or self._persistent_restore_inv is not None:
                self.memory_probe._append(
                    f"SAVEFILE WRITE SEEN old={old_sig} new={final_sig} but watermark not advanced "
                    f"reason=restore_in_progress reconciling={self._restore_reconciling} "
                    f"restore_pending={self._restore_batch_remaining}\n"
                )
                continue

            room = self.memory_probe._last_room
            self.saved_delivered_indices = set(self.delivered_indices)
            self.save_watermark_known = True
            self._save_journal()
            self.memory_probe._append(
                f"SAVEFILE WRITE CONFIRMED room={room} old={old_sig} new={final_sig}\n"
                f"SAVE WATERMARK recorded room={room} delivered_count={len(self.saved_delivered_indices)} "
                f"normal_pending={[d.index for d in self.pending if not d.is_restore]}\n"
            )
            logger.info(
                "RE0 save file changed; watermark recorded with %d delivered AP indexes.",
                len(self.saved_delivered_indices),
            )

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self._ap_server_seed_name = str(args.get("seed_name") or "unknown-seed")
            self.memory_probe._append(
                f"AP_ROOMINFO seed={self._ap_server_seed_name} (0.6.7-compatible journal identity)\n"
            )
        elif cmd == "Connected":
            slot_data = args.get("slot_data") or {}
            randomize_ribbons = bool(slot_data.get("randomize_ink_ribbons", True))
            self.active_location_count = int(slot_data.get("normal_location_count", len(LOCATION_DEFS)))
            self.memory_probe.set_disabled_location_ids(
                set() if randomize_ribbons else set(INK_RIBBON_LOCATION_IDS)
            )
            try:
                self._load_journal(args)
            except Exception as exc:
                # Journal metadata must never be allowed to tear down the AP socket.
                # Continue connected and make the failure visible in both logs.
                logger.exception("RE0 journal initialization failed: %s", exc)
                self.memory_probe._append(f"JOURNAL_INIT_ERROR {exc!r}\n")
            logger.info("Connected to Archipelago as %s.", self.auth)
            self.memory_probe._append(
                f"AP_CONNECTED auth={self.auth} slot={self.slot} seed={self._ap_server_seed_name or 'unknown-seed'}\n"
            )
            checked = args.get("checked_locations", [])
            self.reported_checks = {int(x) for x in checked}
            logger.info(
                "RE0 split-client build: server already has %d / %d checks for this slot. "
                "No ASI/DLL bridge is used.",
                len(self.reported_checks), self.active_location_count,
            )
        elif cmd == "ReceivedItems":
            start_index = int(args.get("index", 0))
            for offset, raw_item in enumerate(args.get("items", [])):
                index = start_index + offset
                item = NetworkItem(*raw_item)
                mapping = AP_TO_RE0.get(item.item)
                item_name = self.item_names.lookup_in_game(item.item)
                if mapping is None:
                    logger.warning(
                        "Received %s (AP id %s), but its RE0 item id is not mapped. Leaving it pending outside the game.",
                        item_name, item.item,
                    )
                    continue
                re0_id, count, slots = mapping
                preferred = None
                # NetworkItem.player is the source player. For a local-source check
                # we know exactly which RE0 character performed that check, so put
                # the returned item back on that same character when possible.
                if self.slot is not None and int(item.player) == int(self.slot):
                    preferred = self.memory_probe.preferred_character_for_location(int(item.location))

                self.delivery_history[index] = {
                    "ap_item_id": int(item.item),
                    "re0_item_id": int(re0_id),
                    "count": int(count),
                    "slots": int(slots),
                    "name": item_name,
                    "source_location": int(item.location),
                    "source_player": int(item.player),
                }
                if index in self.queued_indices or index in self.delivered_indices:
                    continue
                self.pending.append(
                    PendingDelivery(
                        index, int(item.item), int(re0_id), int(count), int(slots), item_name,
                        int(item.location), int(item.player), preferred, False,
                    )
                )
                self.queued_indices.add(index)
                logger.info(
                    "Queued AP item #%d for direct RE0 delivery: %s -> item %d x%d (%d slot%s)%s",
                    index, item_name, re0_id, count, slots, "" if slots == 1 else "s",
                    " to source-check character" if preferred in (0, 1) else "",
                )
            self._save_journal()

    async def delivery_loop(self):
        last_status = None
        while not self.exit_event.is_set():
            # Serialize reload handling: once gameplay disappears, ordinary server
            # deliveries must not race the restore loop.  v1.6 released them as soon
            # as memory became stable; a queued progression item could then enter
            # delivered_indices milliseconds before restore analysis and be replayed
            # a second time.
            if self._restore_reconciling:
                self.delivery_status = "save/load reconciliation - normal delivery paused"
                await asyncio.sleep(0.05)
                continue

            if not self.pending:
                self.delivery_status = "ready - queue empty" if self.memory_probe.handle else "waiting for RE0"
                await asyncio.sleep(0.20)
                continue

            delivery = self.pending[0]
            status, detail, used_char = self.memory_probe.direct_deliver(
                delivery.index,
                delivery.re0_item_id,
                delivery.count,
                delivery.slots,
                delivery.preferred_char,
            )
            if status == "OK":
                self.pending.pop(0)
                if not delivery.is_restore:
                    self.queued_indices.discard(delivery.index)
                    self.delivered_indices.add(delivery.index)
                    self._save_journal()
                elif self._restore_batch_remaining > 0:
                    self._restore_batch_remaining -= 1
                    if self._restore_batch_remaining == 0:
                        self._clear_persistent_restore_snapshot()
                self.delivery_status = f"ready - last delivery {detail}"
                logger.info("Delivered to RE0 via Python: %s (%s)", delivery.display_name, detail)
                last_status = None
                # v1.10: restore jobs are intentionally paced.  RE0 gets time to
                # observe/normalize each inventory write before the next historical
                # AP delivery is replayed.  Normal live AP delivery stays fast.
                await asyncio.sleep(0.50 if delivery.is_restore else 0.03)
                continue

            if status == "FULL":
                self.delivery_status = f"inventory full - {detail}"
                msg = f"FULL {delivery.display_name}: {detail}"
                if last_status != msg:
                    logger.warning("RE0 inventory is full; %s remains pending (%s).", delivery.display_name, detail)
                    last_status = msg
                await asyncio.sleep(0.50)
                continue

            if status == "WAIT":
                self.delivery_status = detail
                await asyncio.sleep(0.25)
                continue

            self.delivery_status = f"delivery error - {detail}"
            msg = f"ERR {delivery.display_name}: {detail}"
            if last_status != msg:
                logger.warning("RE0 direct delivery error for %s: %s", delivery.display_name, detail)
                last_status = msg
            await asyncio.sleep(0.50)

    @staticmethod
    def _char_total(raw: bytes, char_index: int, item_id: int) -> int:
        total = 0
        for si in range(6):
            slot = RE0MemoryProbe._slot(raw, char_index, si)
            it, qty = RE0MemoryProbe._slot_vals(slot)
            if it == item_id:
                total += qty
        return total

    @classmethod
    def _inventory_total(cls, raw: bytes, item_id: int) -> int:
        return cls._char_total(raw, 0, item_id) + cls._char_total(raw, 1, item_id)

    @staticmethod
    def _dynamic_floor_total(table: bytes | None, item_id: int) -> int:
        if table is None:
            return 0
        total = 0
        for i in range(DYNAMIC_RECORD_START, RECORD_COUNT):
            off = i * RECORD_STRIDE
            rec = table[off:off + RECORD_STRIDE]
            if len(rec) != RECORD_STRIDE:
                continue
            d = RE0MemoryProbe._record_fields(rec)
            if (
                d["type"] == WORLD_ITEM_TYPE
                and d["room"] != 0xFFFFFFFF
                and d["item"] == item_id
            ):
                total += int(d["qty"])
        return total

    @classmethod
    def _owned_total(cls, inv: bytes, table: bytes | None, item_id: int) -> int:
        # AP items in RE0 may be carried by either character or deliberately left
        # on the floor.  Treat all three places as one inventory for save/load
        # persistence so a dropped Microfilm/Key is not duplicated on reload.
        return cls._inventory_total(inv, item_id) + cls._dynamic_floor_total(table, item_id)

    async def restore_loop(self):
        """Restore AP-delivered state lost by RE0 save/load or a process crash.

        A load/death/crash is detected when gameplay memory disappears and returns,
        even under a new re0hd.exe PID. We compare the last stable pre-loss ownership
        state with the newly stable loaded state, counting Rebecca + Billy + the
        runtime floor/drop pool together. Only deficits for item types this AP slot
        actually delivered are restored, capped by the delivered AP quantity.
        """
        while not self.exit_event.is_set():
            if not self.memory_probe.load_restore_events:
                await asyncio.sleep(0.10)
                continue

            event = self.memory_probe.load_restore_events.popleft()
            if len(event) == 4:
                pre, post, pre_table, post_table = event
            else:
                # Backward-safe fallback for an event queued by an older in-memory
                # instance during a hot swap.
                pre, post = event
                pre_table = post_table = None
            # IMPORTANT: reconcile against the frozen set captured *before* the
            # load/crash, never the live delivered_indices set.  Normal AP delivery
            # is paused until this plan is complete, but the frozen basis also makes
            # the journal restart-safe and documents exactly what state we are
            # reconstructing.
            basis_indices = set(self._restore_delivered_indices)
            if not basis_indices and self._persistent_restore_inv is None:
                # Defensive fallback for a legacy in-memory event with no persisted
                # v1.8 snapshot.  Fresh v1.8 events always have a frozen basis.
                basis_indices = set(self.delivered_indices)
            normal_pending = [d.index for d in self.pending if not d.is_restore]
            self.memory_probe._append(
                f"LOAD_RESTORE PLAN basis_delivered={sorted(basis_indices)} "
                f"saved={sorted(self.saved_delivered_indices)} normal_pending={normal_pending}\n"
            )

            # v1.10: keep restore ownership arithmetic global by RE0 item ID, but
            # NEVER collapse multiple AP deliveries into one synthetic inventory
            # write.  RE0 stores non-stackable objects (herbs, keys, etc.) as one
            # object per slot even though the slot has a numeric quantity field.
            # Writing Green Herb qty=3 creates one herb-shaped slot, not three
            # independent herbs.  Build the deficit per item type, then decompose
            # that deficit back into the original AP delivery records in AP index
            # order.  This also makes reload behavior mirror the original receive
            # order instead of bulk-injecting an aggregated item type.
            sources_by_re0: dict[int, list[tuple[int, dict]]] = {}
            for index in sorted(basis_indices):
                info = self.delivery_history.get(index)
                if not info:
                    continue
                rid = int(info["re0_item_id"])
                sources_by_re0.setdefault(rid, []).append((index, info))

            # Descriptors are sorted globally by the original AP receive index
            # before being prepended to the delivery queue.
            restore_descriptors: list[tuple[int, str, int, int, int, int, str, int, int, int | None]] = []

            for rid, sources in sources_by_re0.items():
                cap = sum(int(info.get("count", 1)) for _index, info in sources)
                before_qty = self._owned_total(pre, pre_table, rid)
                after_qty = self._owned_total(post, post_table, rid)
                deficit = max(0, before_qty - after_qty)
                remaining = min(deficit, cap)
                if remaining <= 0:
                    continue

                # Character placement is only a destination preference; ownership
                # accounting remains global across Rebecca + Billy + dropped floor.
                r_pre = self._char_total(pre, 0, rid)
                b_pre = self._char_total(pre, 1, rid)
                preferred = 0 if r_pre > 0 else (1 if b_pre > 0 else None)

                split_count = 0
                for source_index, info in sources:
                    if remaining <= 0:
                        break
                    source_count = max(0, int(info.get("count", 1)))
                    if source_count <= 0:
                        continue
                    amount = min(source_count, remaining)
                    if amount <= 0:
                        continue
                    restore_descriptors.append(
                        (
                            source_index,
                            "deficit",
                            int(info.get("ap_item_id", 0)),
                            rid,
                            amount,
                            int(info.get("slots", 1)),
                            str(info.get("name", f"RE0 item {rid}")),
                            int(info.get("source_location", 0)),
                            int(info.get("source_player", 0)),
                            preferred,
                        )
                    )
                    split_count += 1
                    remaining -= amount

                self.memory_probe._append(
                    f"LOAD_RESTORE_DEFICIT item={rid} qty={min(deficit, cap)} "
                    f"pre_owned={before_qty} post_owned={after_qty} jobs={split_count} "
                    f"pre_floor={self._dynamic_floor_total(pre_table, rid)} "
                    f"post_floor={self._dynamic_floor_total(post_table, rid)}\n"
                )

            # A progression item can be consumed to create an unsaved world effect
            # (Crank, key, statue/tablet steps, etc.). Such an item is absent from
            # the pre-crash inventory, so a pure inventory-delta restore cannot
            # recover it after loading the older save. When we have a confirmed
            # save watermark, replay only progression deliveries that occurred after
            # that save AND were no longer owned at the moment gameplay was lost.
            # Items still owned pre-loss are already represented by deficit jobs.
            if self.save_watermark_known:
                for index in sorted(basis_indices - self.saved_delivered_indices):
                    info = self.delivery_history.get(index)
                    if not info:
                        continue
                    ap_id = int(info.get("ap_item_id", 0))
                    item_def = ITEM_BY_AP_ID.get(ap_id)
                    if item_def is None or item_def.classification != "progression":
                        continue
                    rid = int(info["re0_item_id"])
                    if self._owned_total(pre, pre_table, rid) > 0:
                        continue
                    if self._owned_total(post, post_table, rid) > 0:
                        continue
                    restore_descriptors.append(
                        (
                            index,
                            "progression",
                            ap_id,
                            rid,
                            int(info.get("count", 1)),
                            int(info.get("slots", 1)),
                            str(info.get("name", f"RE0 item {rid}")),
                            int(info.get("source_location", 0)),
                            int(info.get("source_player", 0)),
                            None,
                        )
                    )
                    self.memory_probe._append(
                        f"LOAD_RESTORE_PROGRESS_REPLAY index={index} item={rid} "
                        f"name={info.get('name', rid)} reason=delivered-after-last-save-and-consumed\n"
                    )

            restore_descriptors.sort(key=lambda job: job[0])
            restore_pending: list[PendingDelivery] = []
            for (
                source_index, kind, ap_id, rid, amount, slots, name,
                source_location, source_player, preferred,
            ) in restore_descriptors:
                self._restore_counter += 1
                synthetic_base = -2_000_000 if kind == "progression" else -1_000_000
                synthetic_index = synthetic_base - self._restore_counter
                label = (
                    f"Post-save progression replay: {name} [AP #{source_index}]"
                    if kind == "progression"
                    else f"Save/load restore: {name} [AP #{source_index}]"
                )
                restore_pending.append(
                    PendingDelivery(
                        synthetic_index,
                        ap_id,
                        rid,
                        amount,
                        slots,
                        label,
                        source_location,
                        source_player,
                        preferred,
                        True,
                    )
                )
                self.memory_probe._append(
                    f"LOAD_RESTORE_JOB source_index={source_index} kind={kind} "
                    f"item={rid} qty={amount} slots={slots}\n"
                )

            queued_count = len(restore_pending)
            queued_any = queued_count > 0
            if queued_any:
                # Preserve AP receive order exactly and keep normal server items
                # behind the entire restore batch.
                self.pending = restore_pending + self.pending

            if queued_any:
                self._restore_batch_remaining += queued_count
                # The restore plan is now immutable and all synthetic deliveries sit
                # at the front of the queue.  Normal AP items may resume only after
                # this point; they can no longer alter the basis used above.
                self._restore_reconciling = False
                self._save_journal()
                self.memory_probe._append(
                    f"LOAD_RESTORE PLAN_READY synthetic={queued_count} "
                    f"normal_pending={normal_pending}\n"
                )
                logger.info("RE0 save/load journal queued AP state for restoration.")
            else:
                self.memory_probe._append("LOAD_RESTORE no AP-delivered inventory/state deficit detected.\n")
                self._clear_persistent_restore_snapshot()

    async def goal_loop(self):
        while not self.exit_event.is_set():
            if self.memory_probe.goal_detected and not self.finished_game:
                try:
                    # The Centurion-cage check can become unavailable after the
                    # MO Disk / sword-door state. If it was missed, cash in its safe
                    # local filler at victory before sending CLIENT_GOAL.
                    if GOAL_PROOF_LOCATION_ID not in self.reported_checks:
                        await self.send_msgs([
                            {"cmd": "LocationChecks", "locations": [GOAL_PROOF_LOCATION_ID]}
                        ])
                        self.reported_checks.add(GOAL_PROOF_LOCATION_ID)
                        logger.info(
                            "Victory proof check sent: %s (%d).",
                            GOAL_PROOF_LOCATION_NAME, GOAL_PROOF_LOCATION_ID,
                        )
                        self.memory_probe._append(
                            f"GOAL_PROOF_CHECK_SENT location={GOAL_PROOF_LOCATION_ID} "
                            f"{GOAL_PROOF_LOCATION_NAME}\n"
                        )

                    await self.send_msgs([
                        {"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}
                    ])
                    self.finished_game = True
                    logger.info("Resident Evil 0 GOAL sent: final Queen Leech ending state confirmed.")
                    self.memory_probe._append("AP_GOAL_SENT ClientStatus.CLIENT_GOAL\n")
                except Exception as exc:
                    logger.warning("Goal detected but AP goal send is waiting for connection: %s", exc)
            await asyncio.sleep(0.10)

    async def memory_location_loop(self):
        """Send record-classified AP checks; direct memory detector is authoritative."""
        last_error = None
        while not self.exit_event.is_set():
            if not self.memory_probe.pending_ap_checks:
                await asyncio.sleep(0.05)
                continue

            location_id = self.memory_probe.pending_ap_checks[0]
            if location_id in self.reported_checks:
                self.memory_probe.pending_ap_checks.popleft()
                self.memory_probe._append(
                    f"AP_CHECK_ALREADY_ON_SERVER location={location_id} "
                    f"{self.location_names.lookup_in_game(location_id)}\n"
                )
                await asyncio.sleep(0.01)
                continue

            location_name = self.location_names.lookup_in_game(location_id)
            try:
                await self.send_msgs([{"cmd": "LocationChecks", "locations": [location_id]}])
                self.reported_checks.add(location_id)
                self.memory_probe.pending_ap_checks.popleft()
                logger.info(
                    "Sent Archipelago location check: %s (%d)",
                    location_name, location_id,
                )
                self.memory_probe._append(
                    f"AP_CHECK_SENT location={location_id} {location_name}\n"
                )
                last_error = None
            except Exception as exc:
                msg = repr(exc)
                if msg != last_error:
                    logger.warning(
                        "Waiting to send RE0 AP check %s: %s",
                        location_name, exc,
                    )
                    last_error = msg
                await asyncio.sleep(0.5)

    def run_gui(self):
        from kvui import GameManager

        class RE0Manager(GameManager):
            logging_pairs = [("Client", "Archipelago"), ("RE0Client", "Resident Evil 0")]
            base_title = "Archipelago Resident Evil 0 Client"

        self.ui = RE0Manager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")


async def main(*launch_args: str):
    parser = get_base_parser()
    args = parser.parse_args(list(launch_args))

    ctx = RE0Context(args.connect, args.password)
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")
    ctx.delivery_task = asyncio.create_task(ctx.delivery_loop(), name="RE0DirectPythonDelivery")
    ctx.location_task = asyncio.create_task(ctx.memory_location_loop(), name="RE0RecordLocations")
    ctx.restore_task = asyncio.create_task(ctx.restore_loop(), name="RE0SaveLoadRestore")
    ctx.save_task = asyncio.create_task(ctx.save_watermark_loop(), name="RE0SaveWatermark")
    ctx.goal_task = asyncio.create_task(ctx.goal_loop(), name="RE0GoalWatcher")
    ctx.probe_task = asyncio.create_task(ctx.memory_probe.loop(ctx.exit_event), name="RE0MemoryDetector")

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    await ctx.exit_event.wait()
    ctx.server_address = None
    for task in (ctx.delivery_task, ctx.location_task, ctx.restore_task, ctx.save_task, ctx.goal_task, ctx.probe_task):
        if task:
            task.cancel()
    ctx._save_journal()
    await ctx.shutdown()


def run_client(*launch_args: str) -> None:
    Utils.init_logging("RE0Client", exception_logger="Client")
    asyncio.run(main(*launch_args))
