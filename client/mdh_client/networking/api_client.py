"""Typed client of the control-plane REST API, with transparent token refresh."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from mdh_client.networking import tls
from mdh_shared import __version__


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        #: Machine-readable context sent by the control plane (e.g. why a PC is not eligible).
        self.details = details or {}


class ApiUnavailable(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(0, "unavailable", message)


class CertificateMismatch(ApiUnavailable):
    """The control plane showed another certificate than the pinned one: a new certificate on
    the VPS (its address must be shared again) or someone in the middle. Never bypassed."""

    def __init__(self) -> None:
        super().__init__("control plane certificate does not match the pinned fingerprint")
        self.code = "certificate_mismatch"


def unreachable(exc: BaseException) -> ApiUnavailable:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return CertificateMismatch()
        cause = cause.__cause__ or cause.__context__
    return ApiUnavailable(f"control plane unreachable: {exc.__class__.__name__}")


def check_base_url(url: str) -> str:
    """Canonical form of a control-plane address (``https://host:port`` plus ``#pin`` if any)."""
    return tls.normalize_address(url)


class ApiClient:
    def __init__(self, address: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 on_refresh_token: Callable[[str | None], None] | None = None) -> None:
        #: As typed by the user, normalised; ``base_url`` is the same without the pin.
        self.address = check_base_url(address)
        self.base_url, _ = tls.split_address(self.address)
        #: Pinned TLS context, shared by HTTP and the WebSockets (None: default verification).
        self.ssl_context = tls.client_context(self.address)
        self._http = httpx.AsyncClient(base_url=self.base_url + "/api/v1", timeout=15.0,
                                       transport=transport, verify=self.ssl_context or True,
                                       headers={"User-Agent": f"MinecraftDistributedHosting/{__version__}"})
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._on_refresh_token = on_refresh_token
        self._refresh_lock = asyncio.Lock()

    @property
    def ws_url(self) -> str:
        u = httpx.URL(self.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        return str(u.copy_with(scheme=scheme)).rstrip("/") + "/api/v1/ws"

    async def close(self) -> None:
        await self._http.aclose()

    def _set_tokens(self, data: dict[str, Any]) -> None:
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        if self._on_refresh_token:
            self._on_refresh_token(self.refresh_token)

    async def _send(self, method: str, path: str, body: Any = None,
                    idempotency_key: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            return await self._http.request(method, path, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise unreachable(exc) from exc

    async def request(self, method: str, path: str, body: Any = None, idempotency_key: str | None = None) -> Any:
        response = await self._send(method, path, body, idempotency_key)
        if response.status_code == 401 and self.refresh_token and not path.startswith("/auth/"):
            await self.refresh()
            response = await self._send(method, path, body, idempotency_key)
        if response.status_code >= 400:
            self._raise_for(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict:
        """Public: the control plane's version and the oldest application it accepts."""
        return await self.request("GET", "/health")

    # --- auth -------------------------------------------------------------------------
    async def register(self, username: str, password: str, invite_code: str | None) -> None:
        await self.request("POST", "/auth/register",
                           {"username": username, "password": password, "invite_code": invite_code})

    async def login(self, username: str, password: str) -> None:
        self._set_tokens(await self.request("POST", "/auth/login",
                                            {"username": username, "password": password}))

    async def refresh(self) -> None:
        async with self._refresh_lock:
            token = self.refresh_token
            if token is None:
                raise ApiError(401, "unauthorized", "not logged in")
            try:
                data = await self.request("POST", "/auth/refresh", {"refresh_token": token})
            except ApiError as exc:
                if exc.status == 401:
                    self.access_token = self.refresh_token = None
                    if self._on_refresh_token:
                        self._on_refresh_token(None)
                raise
            self._set_tokens(data)

    async def logout(self) -> None:
        if self.refresh_token:
            try:
                await self.request("POST", "/auth/logout", {"refresh_token": self.refresh_token})
            except ApiError:
                pass
        self.access_token = self.refresh_token = None
        if self._on_refresh_token:
            self._on_refresh_token(None)

    async def change_password(self, current: str, new: str) -> None:
        await self.request("POST", "/me/password", {"current_password": current, "new_password": new})

    async def alerts(self) -> list[dict]:
        return await self.request("GET", "/alerts")

    async def me(self) -> dict:
        return await self.request("GET", "/me")

    # --- groups -----------------------------------------------------------------------
    async def create_group(self, name: str) -> dict:
        return await self.request("POST", "/groups", {"name": name})

    async def join_group(self, code: str) -> dict:
        return await self.request("POST", "/groups/join", {"code": code})

    async def create_invite(self, group_id: str, max_uses: int = 1) -> dict:
        return await self.request("POST", f"/groups/{group_id}/invites", {"max_uses": max_uses})

    async def members(self, group_id: str) -> list[dict]:
        return await self.request("GET", f"/groups/{group_id}/members")

    async def update_member(self, group_id: str, user_id: str, **changes: Any) -> None:
        await self.request("PATCH", f"/groups/{group_id}/members/{user_id}", changes)

    async def remove_member(self, group_id: str, user_id: str) -> bool:
        """Remove a member, or leave (``user_id`` = oneself). True: pending, their PC hosts a
        server of the group, which first moves to another PC or stops."""
        return bool((await self.request("DELETE", f"/groups/{group_id}/members/{user_id}") or {}).get("pending"))

    async def transfer_ownership(self, group_id: str, user_id: str, current_password: str) -> None:
        await self.request("POST", f"/groups/{group_id}/owner",
                           {"user_id": user_id, "current_password": current_password})

    # --- hosts ------------------------------------------------------------------------
    async def hosts(self) -> list[dict]:
        return await self.request("GET", "/hosts")

    async def register_host(self, display_name: str, os_name: str, cpu_cores: int, total_ram_mb: int) -> dict:
        return await self.request("POST", "/hosts", {"display_name": display_name, "os": os_name[:64],
                                                     "cpu_cores": cpu_cores, "total_ram_mb": total_ram_mb})

    async def update_host(self, host_id: str, **changes: Any) -> dict:
        return await self.request("PATCH", f"/hosts/{host_id}", changes)

    async def server_hosts(self, server_id: str) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/hosts")

    async def set_priority(self, server_id: str, host_id: str, priority: int, enabled: bool = True) -> None:
        await self.request("PUT", f"/servers/{server_id}/hosts/{host_id}/priority",
                           {"priority": priority, "enabled": enabled})

    # --- servers ----------------------------------------------------------------------
    async def servers(self, group_id: str) -> list[dict]:
        return await self.request("GET", f"/groups/{group_id}/servers")

    async def server(self, server_id: str) -> dict:
        return await self.request("GET", f"/servers/{server_id}")

    async def create_server(self, group_id: str, **config: Any) -> dict:
        return await self.request("POST", f"/groups/{group_id}/servers", config)

    async def update_server(self, server_id: str, **changes: Any) -> dict:
        return await self.request("PATCH", f"/servers/{server_id}", changes)

    async def start(self, server_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/start")

    async def stop(self, server_id: str, countdown_s: int = 30) -> dict:
        return await self.request("POST", f"/servers/{server_id}/stop", {"countdown_s": countdown_s})

    async def transfer(self, server_id: str, target_host_id: str | None = None, countdown_s: int = 60) -> dict:
        return await self.request("POST", f"/servers/{server_id}/transfer",
                                  {"target_host_id": target_host_id, "countdown_s": countdown_s})

    async def cancel_transfer(self, server_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/transfer/cancel")

    async def send_action(self, server_id: str, action: dict) -> dict:
        """An admin action from the closed list (docs/API.md section 4 bis)."""
        return await self.request("POST", f"/servers/{server_id}/actions", {"action": action})

    async def activity(self, server_id: str) -> dict:
        return await self.request("GET", f"/servers/{server_id}/activity")

    async def report_activity(self, server_id: str, host_id: str, epoch: int, players: list[str],
                              lines: list[dict]) -> None:
        await self.request("POST", f"/servers/{server_id}/activity",
                           {"host_id": host_id, "epoch": epoch, "players": players, "lines": lines})

    async def report_console(self, server_id: str, host_id: str, epoch: int, lines: list[dict]) -> None:
        await self.request("POST", f"/servers/{server_id}/console",
                           {"host_id": host_id, "epoch": epoch, "lines": lines})

    async def console(self, server_id: str, after: int = 0) -> dict:
        return await self.request("GET", f"/servers/{server_id}/console?after={int(after)}")

    async def access_requests(self, server_id: str) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/access-requests")

    async def answer_access_request(self, server_id: str, player: str, accept: bool,
                                    place: str | None = None) -> dict:
        verb = "accept" if accept else "refuse"
        return await self.request("POST", f"/servers/{server_id}/access-requests/{player}/{verb}",
                                  None if place is None else {"place": place})

    async def stats(self, server_id: str, days: int = 30, utc_offset_min: int = 0) -> dict:
        return await self.request("GET", f"/servers/{server_id}/stats?days={days}&utc_offset_min={utc_offset_min}")

    async def player_addresses(self, server_id: str) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/player-addresses")

    async def regenerate_player_address(self, server_id: str, player: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/player-addresses/{player}/regenerate")

    async def confirm_failover(self, server_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/failover/confirm")

    async def events(self, server_id: str, limit: int = 50) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/events?limit={limit}")

    async def metrics(self, server_id: str) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/metrics")

    async def performance(self, server_id: str, hours: int = 24) -> dict:
        return await self.request("GET", f"/servers/{server_id}/performance?hours={hours}")

    async def transfer_addresses(self, server_id: str, host_id: str, epoch: int) -> list[dict]:
        return await self.request("POST", f"/servers/{server_id}/transfer-addresses",
                                  {"host_id": host_id, "epoch": epoch})

    async def speedtest(self, host_id: str, data: bytes) -> dict:
        """Random bytes to the VPS, which only counts them: the caller times the upload."""
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/octet-stream"}
        try:
            response = await self._http.post(f"/hosts/{host_id}/speedtest", content=data, headers=headers,
                                             timeout=120)
        except httpx.TransportError as exc:
            raise unreachable(exc) from exc
        if response.status_code >= 400:
            self._raise_for(response)
        return response.json()

    async def config_files(self, server_id: str) -> dict[str, str]:
        return await self.request("GET", f"/servers/{server_id}/config-files")

    # --- world storage ----------------------------------------------------------------
    async def missing_blobs(self, group_id: str, hashes: list[str]) -> list[str]:
        missing: list[str] = []
        for i in range(0, len(hashes), 10_000):
            data = await self.request("POST", f"/groups/{group_id}/blobs/missing", {"hashes": hashes[i:i + 10_000]})
            missing += data["missing"]
        return missing

    async def put_blob(self, group_id: str, sha256: str, compressed: bytes, *, server_id: str,
                       host_id: str, epoch: int) -> None:
        response = await self._raw("PUT", f"/groups/{group_id}/blobs/{sha256}", content=compressed,
                                   params={"server_id": server_id, "host_id": host_id, "epoch": epoch})
        self._raise_for(response)

    async def get_blob(self, group_id: str, sha256: str) -> bytes:
        response = await self._raw("GET", f"/groups/{group_id}/blobs/{sha256}")
        self._raise_for(response)
        return response.content

    async def commit_version(self, server_id: str, idempotency_key: str | None = None, **body: Any) -> dict:
        return await self.request("POST", f"/servers/{server_id}/versions", body, idempotency_key)

    async def versions(self, server_id: str, limit: int = 100) -> list[dict]:
        return await self.request("GET", f"/servers/{server_id}/versions?limit={limit}")

    async def manifest(self, server_id: str, version_id: str) -> dict:
        return await self.request("GET", f"/servers/{server_id}/versions/{version_id}/manifest")

    async def pin_version(self, server_id: str, version_id: str, pinned: bool, label: str | None = None) -> dict:
        return await self.request("POST", f"/servers/{server_id}/versions/{version_id}/pin",
                                  {"pinned": pinned, "label": label})

    async def restore(self, server_id: str, version_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/restore", {"version_id": version_id})

    async def delete_server(self, server_id: str, name: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/delete", {"name": name})

    async def new_world(self, server_id: str, seed: str = "", level_type: str = "normal",
                        generate_structures: bool = True) -> dict:
        return await self.request("POST", f"/servers/{server_id}/new-world",
                                  {"seed": seed, "level_type": level_type,
                                   "generate_structures": generate_structures})

    async def storage(self, group_id: str) -> dict:
        return await self.request("GET", f"/groups/{group_id}/storage")

    async def _raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
            try:
                response = await self._http.request(method, path, headers=headers, timeout=60.0, **kwargs)
            except httpx.TransportError as exc:
                raise unreachable(exc) from exc
            if response.status_code == 401 and self.refresh_token and attempt == 0:
                await self.refresh()
                continue
            return response
        return response

    @staticmethod
    def _raise_for(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            err = response.json()["error"]
            error = ApiError(response.status_code, err["code"], err["message"], err.get("details"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ApiError(response.status_code, "http_error", response.text[:200]) from None
        raise error

    # --- application updates ---------------------------------------------------------
    async def client_release(self, platform: str = "windows") -> dict | None:
        """The installer published on the control plane for this system, or None."""
        try:
            return await self.request("GET", "/client/release" + _platform_query(platform))
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    async def download_client_installer(self, dest: Path, max_bytes: int,
                                        platform: str = "windows") -> tuple[int, str]:
        """Stream the published installer into ``dest``; returns (size, SHA-256).

        Stops reading past ``max_bytes``: the caller compares with the signed size anyway.
        """
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
            try:
                async with self._http.stream("GET", "/client/release/installer" + _platform_query(platform),
                                             headers=headers,
                                             timeout=60.0) as response:
                    if response.status_code == 401 and self.refresh_token and attempt == 0:
                        await self.refresh()
                        continue
                    if response.status_code >= 400:
                        await response.aread()
                        self._raise_for(response)
                    digest, size = hashlib.sha256(), 0
                    with dest.open("wb") as fh:
                        async for chunk in response.aiter_bytes(1 << 20):
                            size += len(chunk)
                            if size > max_bytes:
                                break
                            digest.update(chunk)
                            fh.write(chunk)
                    return size, digest.hexdigest()
            except httpx.TransportError as exc:
                raise unreachable(exc) from exc
        raise ApiError(401, "unauthorized", "not logged in")

    # --- lease (host side) ------------------------------------------------------------
    async def acquire(self, server_id: str, host_id: str, offer_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/lease/acquire",
                                  {"host_id": host_id, "offer_id": offer_id})

    async def start_import(self, server_id: str, host_id: str) -> dict:
        return await self.request("POST", f"/servers/{server_id}/import", {"host_id": host_id})

    async def renew(self, server_id: str, host_id: str, epoch: int) -> dict:
        return await self.request("POST", f"/servers/{server_id}/lease/renew",
                                  {"host_id": host_id, "epoch": epoch})

    async def release(self, server_id: str, host_id: str, epoch: int,
                      final_version_id: str | None = None) -> None:
        await self.request("POST", f"/servers/{server_id}/lease/release",
                           {"host_id": host_id, "epoch": epoch, "final_version_id": final_version_id})

    async def host_state(self, server_id: str, host_id: str, epoch: int, state: str,
                         detail: str | None = None, crash: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"host_id": host_id, "epoch": epoch, "state": state,
                                "detail": detail[:500] if detail else None}
        if crash is not None:
            body["crash"] = crash
        await self.request("POST", f"/servers/{server_id}/host-state", body)


def _platform_query(platform: str) -> str:
    """Windows asks without it, exactly as before 0.9.0."""
    return "" if platform == "windows" else f"?platform={platform}"
