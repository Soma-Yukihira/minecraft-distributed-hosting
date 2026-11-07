"""The sync engine against a real control plane: integrity, resumption, recovery."""

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
import websockets
import zstandard

from mdh_client.networking.api_client import ApiClient, ApiUnavailable
from mdh_client.synchronization import engine as engine_module
from mdh_client.synchronization.engine import LeaseContext, LeaseLost, NotEnoughSpace, SyncEngine, SyncError
from mdh_shared import __version__
from mdh_shared.constants import BLOCK_SIZE
from mdh_shared.sync.manifest_builder import build_manifest, verify_directory
from test_agent_live import PW, LiveBackend

AVAIL = {"hosting_allowed": True, "free_ram_mb": 20000, "free_disk_mb": 500000, "relay_ok": True}


@pytest.fixture
def backend(tmp_path):
    b = LiveBackend(tmp_path)
    b.start()
    yield b
    if b.server and not b.server.should_exit:
        b.stop()


def make_world(root: Path, seed: int = 0) -> None:
    rnd = __import__("random").Random(seed)
    (root / "world" / "region").mkdir(parents=True, exist_ok=True)
    (root / "world" / "region" / "r.0.0.mca").write_bytes(rnd.randbytes(2 * BLOCK_SIZE + 12345))
    (root / "world" / "level.dat").write_bytes(b"level-" + str(seed).encode())
    (root / "server.properties").write_text("motd=test\n")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "latest.log").write_text("never synced")


def snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".mdh" not in p.parts}


class Host:
    """Holds a real lease like a PC does: websocket, offer, acquire, then renewals."""

    def __init__(self, backend: LiveBackend) -> None:
        self.api = ApiClient(backend.url)

    async def setup(self) -> None:
        await self.api.register("oscar", PW, None)
        await self.api.login("oscar", PW)
        self.group = (await self.api.create_group("g"))["group_id"]
        self.host = (await self.api.register_host("PC", "Windows", 8, 32768))["host_id"]
        self.sid = (await self.api.create_server(self.group, name="s", mc_version="26.3",
                                                 memory_mb=1024))["server_id"]
        self.ws = await websockets.connect(self.api.ws_url)
        await self.ws.send(json.dumps({"type": "hello", "access_token": self.api.access_token,
                                       "host_id": self.host, "client_version": __version__}))
        await self.ws.recv()
        await self.ws.send(json.dumps({"type": "heartbeat", "seq": 1, "availability": AVAIL}))
        await self.api.start(self.sid)
        while True:
            msg = json.loads(await self.ws.recv())
            if msg["type"] == "hosting_offer":
                break
        self.epoch = (await self.api.acquire(self.sid, self.host, msg["offer_id"]))["epoch"]
        self.renewing = asyncio.create_task(self._renew())

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(1.5)
            with contextlib.suppress(Exception):
                await self.api.renew(self.sid, self.host, self.epoch)

    @property
    def ctx(self) -> LeaseContext:
        return LeaseContext(self.group, self.sid, self.host, self.epoch)

    async def head(self) -> dict | None:
        versions = await self.api.versions(self.sid)
        return versions[0] if versions else None

    async def close(self) -> None:
        self.renewing.cancel()
        await self.ws.close()
        await self.api.close()


def run(backend, scenario):
    async def main():
        host = Host(backend)
        await host.setup()
        try:
            await scenario(host)
        finally:
            await host.close()

    asyncio.run(main())


def test_full_then_incremental_sync(tmp_path, backend) -> None:
    a, b = tmp_path / "pc_a", tmp_path / "pc_b"
    make_world(a)

    async def scenario(host: Host):
        engine = SyncEngine(host.api)
        v1 = await engine.upload(host.ctx, a, parent=None, kind="final")
        result = await engine.materialize(host.group, host.sid, v1, b)
        assert result.changed_files == 3 and result.downloaded_blocks == 5
        verify_directory(b, build_manifest(a)[0])
        assert not (b / "logs").exists()                          # never synchronised

        # One chunk of the region file changes on PC A: one block travels each way.
        region = a / "world" / "region" / "r.0.0.mca"
        data = bytearray(region.read_bytes())
        data[BLOCK_SIZE + 10:BLOCK_SIZE + 4106] = os.urandom(4096)
        region.write_bytes(bytes(data))
        sent: list[str] = []
        original = host.api.put_blob

        async def counting(group, sha, compressed, **kw):
            sent.append(sha)
            return await original(group, sha, compressed, **kw)

        host.api.put_blob = counting
        v2 = await engine.upload(host.ctx, a, parent=v1["version_id"], kind="hot")
        assert len(sent) == 1 and v2["parent_version_id"] == v1["version_id"]
        result = await engine.materialize(host.group, host.sid, v2, b)
        assert (result.changed_files, result.downloaded_blocks, result.reused_blocks) == (1, 1, 2)
        assert (b / "world/region/r.0.0.mca").read_bytes() == bytes(data)
        assert engine.local_version(b) == v2["version_id"] and not await engine.has_local_changes(b)
        # Already up to date: nothing moves.
        assert (await engine.materialize(host.group, host.sid, v2, b)).changed_files == 0

    run(backend, scenario)


def test_interrupted_upload_resumes_without_resending(tmp_path, backend, monkeypatch) -> None:
    a = tmp_path / "pc_a"
    make_world(a)
    monkeypatch.setattr(engine_module.asyncio, "sleep", _fast_sleep)

    async def scenario(host: Host):
        engine = SyncEngine(host.api, concurrency=1)
        original = host.api.put_blob
        calls: list[str] = []

        async def flaky(group, sha, compressed, **kw):
            if len(calls) >= 2:
                raise ApiUnavailable("network cut")
            calls.append(sha)
            return await original(group, sha, compressed, **kw)

        host.api.put_blob = flaky
        with pytest.raises(ApiUnavailable):
            await engine.upload(host.ctx, a, parent=None, kind="final")
        assert await host.head() is None                          # nothing committed
        host.api.put_blob = original
        resent: list[str] = []

        async def counting(group, sha, compressed, **kw):
            resent.append(sha)
            return await original(group, sha, compressed, **kw)

        host.api.put_blob = counting
        v1 = await engine.upload(host.ctx, a, parent=None, kind="final")
        assert len(resent) == 5 - 2 and not set(resent) & set(calls)
        assert (await host.head())["version_id"] == v1["version_id"]

    run(backend, scenario)


async def _fast_sleep(_delay):
    await _real_sleep(0)


_real_sleep = asyncio.sleep


def test_corrupted_block_on_the_vps_never_reaches_the_world(tmp_path, backend) -> None:
    a, b = tmp_path / "pc_a", tmp_path / "pc_b"
    make_world(a, seed=1)
    make_world(b, seed=2)                                         # PC B has another, older world
    before = snapshot_bytes(b)

    async def scenario(host: Host):
        engine = SyncEngine(host.api)
        v1 = await engine.upload(host.ctx, a, parent=None, kind="final")
        sha = hashlib.sha256((a / "world" / "level.dat").read_bytes()).hexdigest()
        stored = backend.db.parent / "storage" / host.group / "blobs" / sha[:2] / sha[2:4] / f"{sha}.zst"
        stored.write_bytes(zstandard.ZstdCompressor().compress(b"bit rot"))   # disk corruption on the VPS
        with pytest.raises(SyncError, match="corrupted"):
            await engine.materialize(host.group, host.sid, v1, b)

    run(backend, scenario)
    assert snapshot_bytes(b) == before                            # live world untouched


def test_interrupted_swap_is_rolled_back(tmp_path, backend, monkeypatch) -> None:
    a, b = tmp_path / "pc_a", tmp_path / "pc_b"
    make_world(a, seed=1)
    make_world(b, seed=2)
    before = snapshot_bytes(b)

    async def scenario(host: Host):
        engine = SyncEngine(host.api)
        v1 = await engine.upload(host.ctx, a, parent=None, kind="final")
        real_replace = engine_module._replace
        moves = {"n": 0}

        def crashing_replace(src, dst):
            into_live = Path(dst).is_relative_to(b) and ".mdh" not in Path(dst).relative_to(b).parts
            if into_live:
                moves["n"] += 1
                if moves["n"] == 2:     # power cut after one file of the new world was put in place
                    raise OSError("power loss")
            real_replace(src, dst)

        monkeypatch.setattr(engine_module, "_replace", crashing_replace)
        with pytest.raises(OSError):
            await engine.materialize(host.group, host.sid, v1, b)
        monkeypatch.setattr(engine_module, "_replace", real_replace)
        assert snapshot_bytes(b) != before                        # half-swapped on disk...
        assert engine.recover(b)                                  # ...until the next start
        assert snapshot_bytes(b) == before
        assert not engine.recover(b)
        await engine.materialize(host.group, host.sid, v1, b)     # and the retry succeeds
        verify_directory(b, build_manifest(a)[0])

    run(backend, scenario)


def test_not_enough_disk_space_is_detected_before_touching_anything(tmp_path, backend, monkeypatch) -> None:
    a, b = tmp_path / "pc_a", tmp_path / "pc_b"
    make_world(a)

    async def scenario(host: Host):
        engine = SyncEngine(host.api)
        v1 = await engine.upload(host.ctx, a, parent=None, kind="final")
        monkeypatch.setattr(engine_module.psutil, "disk_usage", lambda p: type("U", (), {"free": 10_000_000})())
        with pytest.raises(NotEnoughSpace):
            await engine.materialize(host.group, host.sid, v1, b)
        assert not (b / "world").exists()

    run(backend, scenario)


def test_lost_lease_refuses_uploads(tmp_path, backend) -> None:
    a = tmp_path / "pc_a"
    make_world(a)

    async def scenario(host: Host):
        host.renewing.cancel()
        await asyncio.sleep(7)                                    # TTL 6 s without renewal
        with pytest.raises(LeaseLost):
            await SyncEngine(host.api).upload(host.ctx, a, parent=None, kind="final")
        assert await host.head() is None

    run(backend, scenario)


def test_divergent_copy_and_snapshot_mirror(tmp_path) -> None:
    live, snap = tmp_path / "live", tmp_path / "snap"
    make_world(live)
    engine = SyncEngine(api=None)  # type: ignore[arg-type]
    SyncEngine.refresh_snapshot(live, snap)
    assert {k: v for k, v in snapshot_bytes(snap).items()} == {
        k: v for k, v in snapshot_bytes(live).items() if not k.startswith("logs")}
    (live / "world" / "level.dat").unlink()
    SyncEngine.refresh_snapshot(live, snap)
    assert not (snap / "world" / "level.dat").exists()

    target = engine.archive_divergent(live)
    assert (target / "world" / "region" / "r.0.0.mca").exists()
    names = [f.path for f in build_manifest(live)[0].files]
    assert not any(n.startswith("divergent/") for n in names)      # never synchronised


def test_throttling_is_waited_out_not_treated_as_a_failure(monkeypatch) -> None:
    from mdh_client.networking.api_client import ApiError
    from mdh_client.synchronization import engine as engine_mod

    async def no_wait(_):
        return None
    monkeypatch.setattr(engine_mod.asyncio, "sleep", no_wait)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 6:
            raise ApiError(429, "rate_limited", "slow down")
        return "ok"

    eng = engine_mod.SyncEngine(api=None)
    assert asyncio.run(eng._call(flaky)) == "ok" and len(calls) == 6

    async def forbidden():
        raise ApiError(403, "forbidden", "no")
    with pytest.raises(ApiError):
        asyncio.run(eng._call(forbidden))                        # real refusals are not retried