# Resident Evil 0 Archipelago Client

Standalone game client for the unofficial **Resident Evil 0 HD Remaster (Steam/PC)** Archipelago world.

This repository contains the game-integration/runtime side only.

## What the client does

The client:
- connects to an Archipelago room
- attaches to the local `re0hd.exe` process
- detects Resident Evil 0 pickups/checks
- sends completed checks to the Archipelago server
- receives Archipelago items
- writes received RE0 items into the local game
- handles queued deliveries
- reconciles delivered items across Resident Evil 0 save/load
- keeps a per-seed journal
- reports victory

No ASI, DLL bridge, Cheat Engine table or executable patch is required.

## Installation

1. Close the Archipelago Launcher.
2. Copy the `re0_client` folder into:
   `<Archipelago install>\lib\worlds\`
3. Re-open the Archipelago Launcher.
4. Launch **Resident Evil 0 Client**.
5. Connect to the Archipelago room.
6. Launch Resident Evil 0 HD Remaster.

The matching `re0.apworld` is distributed separately.

## Save/load journal

The client stores reconciliation state in:

`%TEMP%\RE0_AP_full_journal_<seed>_slot<slot>_<player>.json`

Do not delete the journal during an active seed.

## Known issue

The Training Facility Rebecca/Billy crank-lift sequence can occasionally enter a broken
camera/partner state. If this happens, using the Follow command and moving through the nearby
room transition has been found to reload Billy correctly.

## Development note

This client was developed with substantial AI assistance and validated through repeated
in-game testing, including a full Normal completion and save/load tests.
