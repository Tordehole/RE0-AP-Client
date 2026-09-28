from worlds.LauncherComponents import Component, Type, components
from worlds import LauncherComponents


def launch_client(*args: str) -> None:
    from .client import run_client
    LauncherComponents.launch(run_client, name="RE0Client", args=args)


components.append(
    Component(
        "Resident Evil 0 Client",
        func=launch_client,
        component_type=Type.CLIENT,
        game_name="Resident Evil 0",
        supports_uri=True,
        description="Connect Resident Evil 0 to Archipelago using direct Python memory delivery; no ASI bridge required.",
    )
)
