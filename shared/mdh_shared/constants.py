"""Protocol-wide constants. Changing any of these is a protocol change."""

from typing import Final

API_VERSION: Final = "v1"

# --- Compatibility (docs/API.md section 0) ---------------------------------
#: Oldest application the control plane accepts on its WebSocket (older ones can still log in
#: and download the update). Raise it when the control plane stops understanding an old client.
#: 0.4.0: an older host would ignore the group's mods and load a modded world without them
#: (Fabric drops the blocks and items of missing mods).
#: 0.6.0: an older host would start a modpack without its configuration files, and could not
#: read the pre-generation actions.
#: 0.7.0: an older host would start without the group's datapacks and settings files.
#: 0.13.0: a Paper world synchronised by a 0.13 host has proxies.proxy-protocol on; an older host
#: would not write the header in its tunnels, and Paper would refuse every player.
MIN_CLIENT_VERSION: Final = "0.13.0"
#: Oldest control plane the application works with. Raise it when the application relies on
#: something only a newer control plane has (the VPS is updated before the application).
#: 0.4.2: the server dialog always sends ``personal_addresses``, unknown to older ones.
#: 0.5.0: console, resource pack, requests from a new place.
#: 0.6.0: modpacks, pre-generation, explained crashes.
#: 0.7.0: performance graphs, spark, settings files, datapacks.
#: 0.8.0: transfer addresses, measures of each PC, automatic choice of the host.
#: 0.9.0: one release per system (Windows, macOS).
#: 0.10.0: new world (seed, world type), generate-structures.
MIN_SERVER_VERSION: Final = "0.10.0"

# --- Synchronisation -------------------------------------------------------
#: Fixed block size used to split files. Block id = SHA-256 of the uncompressed block.
BLOCK_SIZE: Final = 1024 * 1024
#: Upper bound on manifest entries (protects the VPS from huge manifests).
MAX_MANIFEST_ENTRIES: Final = 200_000
#: Upper bound on the size of a single synchronised file (region files are far smaller).
MAX_FILE_SIZE: Final = 4 * 1024**3
#: Maximum relative path length. Leaves room under Windows' 260-char MAX_PATH for the base dir.
MAX_PATH_LENGTH: Final = 200
MAX_PATH_COMPONENT_LENGTH: Final = 120
#: Executable content is never synchronised: mods/plugins are declared and fetched from their
#: official source instead (see docs/ARCHITECTURE.md, P7).
FORBIDDEN_EXTENSIONS: Final = frozenset(
    {".jar", ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".com", ".scr", ".lnk",
     ".sh", ".so", ".dylib"}
)
#: Top-level directories of a server folder that are never synchronised.
EXCLUDED_TOP_LEVEL_DIRS: Final = frozenset(
    {"logs", "crash-reports", "cache", "libraries", "versions", ".mdh", "divergent", ".fabric",
     # Rendered world map (BlueMap): large, and each host renders it again from the world.
     "bluemap"}
)
#: Top-level directories holding the world itself (Paper splits the Nether and the End out):
#: a new world drops them and keeps everything else (ops, bans, mod settings...).
WORLD_DIRS: Final = ("world", "world_nether", "world_the_end")
#: File name patterns (any depth) that are never synchronised.
EXCLUDED_FILE_PATTERNS: Final = ("session.lock", "*.tmp", "usercache.json", "*.log", "*.log.gz")

# --- Leases (seconds) -----------------------------------------------------
# Safety condition (docs/ARCHITECTURE.md 2.3):
#   (LEASE_TTL - LEASE_SAFETY_MARGIN) * (1 + drift) + stop_duration < LEASE_TTL + LEASE_GRACE
LEASE_TTL_S: Final = 30.0
LEASE_RENEW_INTERVAL_S: Final = 10.0
LEASE_SAFETY_MARGIN_S: Final = 5.0
LEASE_GRACE_S: Final = 15.0
HEARTBEAT_INTERVAL_S: Final = LEASE_RENEW_INTERVAL_S

# --- Election --------------------------------------------------------------
PRIORITY_MIN: Final = 0
PRIORITY_MAX: Final = 100
#: RAM a host must keep for itself on top of the server allocation.
HOST_RAM_HEADROOM_MB: Final = 2048
#: Disk a host must keep free on top of twice the world size (staging + current copy).
HOST_DISK_HEADROOM_MB: Final = 2048
OFFER_TIMEOUT_S: Final = 30.0

# --- WebSocket -------------------------------------------------------------
MAX_WS_MESSAGE_BYTES: Final = 64 * 1024
