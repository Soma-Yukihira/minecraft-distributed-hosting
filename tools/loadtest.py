"""Load test of the control plane, run as its own process like on the VPS.

    .venv\\Scripts\\python.exe tools\\loadtest.py [--players 50] [--seconds 30] [--world-mb 300]

1. Starts ``python -m mdh_server serve`` (temporary database and storage, relay on a free port).
2. A simulated host takes the lease and answers relay requests with real tunnels to a local echo
   server (stands for Minecraft).
3. N simulated players join through the relay and exchange Minecraft-sized packets (20/s each
   way, like a busy player) while the round-trip time is measured.
4. The host uploads a world of the given size (incompressible random blocks: worst case).

Reports latency, throughput, and the control plane's memory (RSS) and CPU, to compare with the
budget of docs/ARCHITECTURE.md section 5. Nothing outside a temporary directory is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ("shared", "server", "client")]

import httpx  # noqa: E402
import psutil  # noqa: E402
import websockets  # noqa: E402
import zstandard  # noqa: E402

from mdh_client.networking.relay_tunnel import run_tunnel  # noqa: E402
from mdh_shared import __version__  # noqa: E402
from mdh_shared.protocol import minecraft as mc  # noqa: E402

PW = "load-test-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sampler:
    """Peak memory and CPU of the control-plane process."""

    def __init__(self, pid: int) -> None:
        self.proc = psutil.Process(pid)
        self.peak_rss = 0
        self.samples: list[int] = []
        self.task: asyncio.Task | None = None

    def _tree(self) -> list[psutil.Process]:
        # On Windows a venv's python.exe is a launcher that runs the real interpreter as a child.
        return [self.proc, *self.proc.children(recursive=True)]

    async def run(self) -> None:
        while True:
            rss = sum(p.memory_info().rss for p in self._tree())
            self.samples.append(rss)
            self.peak_rss = max(self.peak_rss, rss)
            await asyncio.sleep(0.25)

    def cpu(self) -> float:
        return sum(t.user + t.system for t in (p.cpu_times() for p in self._tree()))


async def main(players: int, seconds: float, world_mb: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mdh-load-"))
    api_port, relay_port = free_port(), free_port()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in ("shared", "server")),
           "MDH_DATABASE_PATH": str(tmp / "mdh.sqlite3"), "MDH_STORAGE_DIR": str(tmp / "storage"),
           "MDH_REGISTRATION_MODE": "open", "MDH_RELAY_BIND": "127.0.0.1", "MDH_RELAY_PORT": str(relay_port),
           "MDH_RELAY_DOMAIN": "mc.test", "MDH_RELAY_MAX_PER_IP": str(players + 10),
           "MDH_RELAY_CONNECTIONS_PER_MINUTE": "100000", "MDH_DEFAULT_GROUP_QUOTA_GB": "50"}
    server = subprocess.Popen([sys.executable, "-m", "mdh_server", "serve", "--bind", f"127.0.0.1:{api_port}"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{api_port}/api/v1"
    try:
        api = httpx.AsyncClient(base_url=base, timeout=60)
        for _ in range(100):
            try:
                if (await api.get("/health")).status_code == 200:
                    break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        sampler = Sampler(server.pid)
        sampler.task = asyncio.create_task(sampler.run())
        await asyncio.sleep(1)
        idle_rss = sampler.samples[-1]

        await api.post("/auth/register", json={"username": "load", "password": PW})
        token = (await api.post("/auth/login", json={"username": "load", "password": PW})).json()["access_token"]
        api.headers["Authorization"] = f"Bearer {token}"
        gid = (await api.post("/groups", json={"name": "load"})).json()["group_id"]
        sid = (await api.post(f"/groups/{gid}/servers", json={"name": "load", "mc_version": "26.3",
                                                              "memory_mb": 1024})).json()["server_id"]
        hid = (await api.post("/hosts", json={"display_name": "pc", "os": "w", "cpu_cores": 8,
                                              "total_ram_mb": 32768})).json()["host_id"]

        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        local = await asyncio.start_server(echo, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        ws_base = f"ws://127.0.0.1:{api_port}/api/v1"

        async with websockets.connect(ws_base + "/ws", max_size=None) as control:
            await control.send(json.dumps({"type": "hello", "access_token": token, "host_id": hid,
                                           "client_version": __version__}))
            await control.recv()
            avail = {"hosting_allowed": True, "free_ram_mb": 20000, "free_disk_mb": 500000, "relay_ok": True}
            seq = [0]
            leases: list[dict] = []

            async def beat_forever():
                while True:
                    seq[0] += 1
                    await control.send(json.dumps({"type": "heartbeat", "seq": seq[0], "leases": leases,
                                                   "availability": avail}))
                    await asyncio.sleep(5)
            beats = asyncio.create_task(beat_forever())
            offers: asyncio.Queue = asyncio.Queue()
            tunnels: list[asyncio.Task] = []

            async def dispatch():
                async for raw in control:
                    msg = json.loads(raw)
                    if msg["type"] == "hosting_offer":
                        await offers.put(msg)
                    elif msg["type"] == "relay_open":
                        tunnels.append(asyncio.create_task(run_tunnel(
                            f"{ws_base}/relay/data/{msg['conn_id']}", msg["relay_token"], local_port)))
            dispatcher = asyncio.create_task(dispatch())
            await api.post(f"/servers/{sid}/start")
            offer = await asyncio.wait_for(offers.get(), 10)
            epoch = (await api.post(f"/servers/{sid}/lease/acquire",
                                    json={"host_id": hid, "offer_id": offer["offer_id"]})).json()["epoch"]
            leases.append({"server_id": sid, "epoch": epoch})
            await api.post(f"/servers/{sid}/host-state", json={"host_id": hid, "epoch": epoch, "state": "running"})

            # ---------------------------------------------------------------- players
            rtts: list[float] = []
            moved = [0]
            payload = os.urandom(200)

            async def player(i: int) -> None:
                reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
                hs = mc.packet(0x00, mc.varint(777) + mc.string("mc.test") + (25565).to_bytes(2, "big") + mc.varint(2))
                login = mc.packet(0x00, mc.string(f"p{i}") + b"\x00" * 16)
                writer.write(hs + login)
                await writer.drain()
                await reader.readexactly(len(hs) + len(login))      # echoed handshake + login
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    t0 = time.perf_counter()
                    writer.write(payload)
                    await writer.drain()
                    await reader.readexactly(len(payload))
                    rtts.append((time.perf_counter() - t0) * 1000)
                    moved[0] += 2 * len(payload)
                    await asyncio.sleep(0.05)                       # 20 packets per second
                writer.close()

            cpu0 = sampler.cpu()
            t0 = time.monotonic()
            await asyncio.gather(*(player(i) for i in range(players)))
            dt = time.monotonic() - t0
            relay_cpu = sampler.cpu() - cpu0
            relay_peak = sampler.peak_rss
            q = statistics.quantiles(rtts, n=100)
            print(f"\n== Relay: {players} players, {seconds:.0f} s, 20 packets/s each way ==")
            print(f"round trip via relay: median {statistics.median(rtts):.2f} ms, p99 {q[98]:.2f} ms, "
                  f"max {max(rtts):.1f} ms ({len(rtts)} samples)")
            print(f"traffic {moved[0] / dt / 1024:.0f} KiB/s; control plane CPU {relay_cpu / dt * 100:.1f} % "
                  f"of one core")

            # ---------------------------------------------------------------- world upload
            blocks = [os.urandom(1024 * 1024) for _ in range(min(world_mb, 64))]
            hashes: list[str] = []
            sampler.peak_rss = 0
            cpu0 = sampler.cpu()
            t0 = time.monotonic()
            sem = asyncio.Semaphore(4)
            comp = zstandard.ZstdCompressor(level=3)

            async def put(i: int) -> None:
                data = blocks[i % len(blocks)][:-8] + i.to_bytes(8, "big")   # unique block each time
                sha = hashlib.sha256(data).hexdigest()
                hashes.append(sha)
                async with sem:
                    r = await api.put(f"/groups/{gid}/blobs/{sha}", content=comp.compress(data),
                                      params={"server_id": sid, "host_id": hid, "epoch": epoch})
                    r.raise_for_status()
            await asyncio.gather(*(put(i) for i in range(world_mb)))
            files = [{"path": f"world/region/r.{i}.0.mca", "size": 1024 * 1024, "sha256": h, "blocks": [h]}
                     for i, h in enumerate(hashes)]
            # File hash == block hash when a file is exactly one block.
            r = await api.post(f"/servers/{sid}/versions", json={
                "host_id": hid, "epoch": epoch, "parent_version_id": None, "kind": "final",
                "manifest": {"files": sorted(files, key=lambda f: f["path"])}})
            dt = time.monotonic() - t0
            print(f"\n== Upload: {world_mb} MiB (incompressible), 4 in parallel ==")
            print(f"commit: HTTP {r.status_code}; {world_mb / dt:.0f} MiB/s; control plane CPU "
                  f"{sampler.cpu() - cpu0:.1f} s ({(sampler.cpu() - cpu0) / world_mb * 1000:.1f} ms per MiB)")
            print(f"\n== Control plane memory (RSS) ==\nidle {idle_rss / 2**20:.0f} MiB; peak with "
                  f"{players} players {relay_peak / 2**20:.0f} MiB; peak during upload "
                  f"{sampler.peak_rss / 2**20:.0f} MiB")
            beats.cancel()
            dispatcher.cancel()
            for t in tunnels:
                t.cancel()
        local.close()
        sampler.task.cancel()
        await api.aclose()
    finally:
        server.terminate()
        server.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--players", type=int, default=50)
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--world-mb", type=int, default=300)
    a = p.parse_args()
    asyncio.run(main(a.players, a.seconds, a.world_mb))
