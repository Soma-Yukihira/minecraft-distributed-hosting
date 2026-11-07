"""Control-plane settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mdh_server.coordination.lease import LeasePolicy
from mdh_server.storage.retention import RetentionPolicy
from mdh_shared.constants import (
    HEARTBEAT_INTERVAL_S,
    LEASE_GRACE_S,
    LEASE_SAFETY_MARGIN_S,
    LEASE_TTL_S,
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/database/mdh.sqlite3"
    storage_dir: str = "data/storage"
    #: Installers published for the automatic updates (install.sh publish-client).
    updates_dir: str = "data/updates"
    registration_mode: Literal["invite", "open"] = "invite"
    lease: LeasePolicy = field(default_factory=LeasePolicy)
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 30 * 24 * 3600
    default_group_quota_bytes: int = 20 * 1024**3
    max_hosts_per_user: int = 10
    #: Peers whose X-Real-IP header is trusted (the local Nginx).
    trusted_proxies: frozenset[str] = frozenset({"127.0.0.1", "::1"})
    #: Run the periodic coordinator sweep. Tests disable it and call ``tick()`` themselves.
    background_tasks: bool = True
    #: Messages one WebSocket session may send (a heartbeat every 10 s and a few answers).
    ws_messages_per_minute: float = 120.0
    ws_messages_burst: int = 30
    sweep_interval_s: float = 2.0
    expose_docs: bool = False
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    #: A migration target must have downloaded the world within this delay (slow home links).
    migration_prefetch_timeout_s: float = 3600.0
    #: A returning higher-priority host must stay online this long before it preempts.
    preemption_stable_s: float = 60.0
    #: After a failed or cancelled migration, wait this long before preempting again.
    preemption_retry_s: float = 600.0
    #: Minecraft relay (docs/ARCHITECTURE.md 4.2): the only port published besides Nginx.
    relay_enabled: bool = True
    relay_bind: str = "0.0.0.0"
    relay_port: int = 25565
    #: Players type ``<domain>`` / ``<label>.<domain>`` / ``<personal token>.<domain>``
    #: (needs a wildcard DNS record ``*.<domain>`` pointing to the VPS).
    relay_domain: str = "localhost"
    relay_max_connections: int = 256
    relay_max_per_ip: int = 8
    relay_connections_per_minute: float = 30.0

    def __post_init__(self) -> None:
        if self.registration_mode not in ("invite", "open"):
            raise ConfigError("MDH_REGISTRATION_MODE must be 'invite' or 'open'")
        # Guard rails for docs/ARCHITECTURE.md 2.3: renewals must fit several times in a lease,
        # and the old host must get a real stop budget before anyone else may start.
        if self.lease.ttl_s < 3 * self.heartbeat_interval_s:
            raise ConfigError("lease TTL must be at least 3 heartbeat intervals")
        if self.lease.ttl_s <= LEASE_SAFETY_MARGIN_S:
            raise ConfigError("lease TTL must exceed the host safety margin")
        if self.lease.grace_s < 10:
            raise ConfigError("lease grace must be at least 10 seconds")
        if not re.fullmatch(r"(?=.{1,200}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?",
                            self.relay_domain):
            raise ConfigError("MDH_RELAY_DOMAIN must be a lower-case host name, e.g. mc.example.org")
        if not 0 <= self.relay_port <= 65535:
            raise ConfigError("MDH_RELAY_PORT must be a TCP port")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def num(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number") from exc

        return cls(
            database_path=env.get("MDH_DATABASE_PATH", cls.database_path),
            storage_dir=env.get("MDH_STORAGE_DIR", cls.storage_dir),
            # Next to the storage folder unless set: /var/lib/mdh/updates on the VPS.
            updates_dir=env.get("MDH_UPDATES_DIR") or str(
                Path(env.get("MDH_STORAGE_DIR", cls.storage_dir)).parent / "updates"),
            registration_mode=env.get("MDH_REGISTRATION_MODE", "invite"),  # type: ignore[arg-type]
            lease=LeasePolicy(
                ttl_s=num("MDH_LEASE_TTL_S", LEASE_TTL_S),
                grace_s=num("MDH_LEASE_GRACE_S", LEASE_GRACE_S),
            ),
            default_group_quota_bytes=int(num("MDH_DEFAULT_GROUP_QUOTA_GB", 20) * 1024**3),
            expose_docs=env.get("MDH_EXPOSE_DOCS", "0") == "1",
            relay_enabled=env.get("MDH_RELAY_ENABLED", "1") == "1",
            relay_bind=env.get("MDH_RELAY_BIND", "0.0.0.0"),
            relay_port=int(num("MDH_RELAY_PORT", 25565)),
            relay_domain=env.get("MDH_RELAY_DOMAIN", "localhost").strip().lower(),
            # Players behind one public IP (a LAN party) share these limits.
            relay_max_per_ip=int(num("MDH_RELAY_MAX_PER_IP", 8)),
            relay_connections_per_minute=num("MDH_RELAY_CONNECTIONS_PER_MINUTE", 30),
            relay_max_connections=int(num("MDH_RELAY_MAX_CONNECTIONS", 256)),
            retention=RetentionPolicy(
                keep_last=int(num("MDH_KEEP_LAST_VERSIONS", 5)),
                hourly_hours=int(num("MDH_KEEP_HOURLY_HOURS", 24)),
                daily_days=int(num("MDH_KEEP_DAILY_DAYS", 7)),
            ),
        )
