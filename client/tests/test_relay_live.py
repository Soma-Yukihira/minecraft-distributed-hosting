"""Players reach whichever PC hosts through one address: the VPS relay (Phase 6).

Real control plane with its relay (domain "mc.test": the tests connect to 127.0.0.1 and send
the hostname in the handshake, so no DNS is needed), real agents, fake Minecraft servers that
listen on their port.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import websockets

from mdh_client.application.agent import HostAgent
from mdh_client.config import ClientSettings, Layout
from mdh_client.networking.relay_tunnel import run_tunnel
from mdh_shared import __version__
from mdh_shared.protocol import minecraft as mc
from test_agent_live import PW, LiveBackend, Recorder, fake_prepare, free_port, plenty_of_resources, until
from test_world_handover import backend, state  # noqa: F401


async def wait_state(agent, sid, wanted, timeout=60):
    """Poll gently: the agent shares the user's API rate limit with this test."""
    for _ in range(int(timeout * 2)):
        if (await state(agent, sid))["state"] == wanted:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"server never became {wanted}")


def pc(tmp_path: Path, backend: LiveBackend, name: str) -> HostAgent:
    layout = Layout(tmp_path / name)
    layout.ensure()
    settings = ClientSettings(api_url=backend.url, display_name=name, max_ram_mb=2048, server_port=free_port(),
                              eula_accepted_at="2026-10-31T10:00:00+00:00")
    return HostAgent(layout, settings, emit=Recorder(), prepare=fake_prepare, store_credentials=False,
                     command_factory=lambda p, mem: [sys.executable, "-u", str(p.jar)],
                     detect_resources=plenty_of_resources, ready_timeout_s=20, stop_timeout_s=10)


# --------------------------------------------------------------------------- a Minecraft client


def handshake(host: str, next_state: int) -> bytes:
    return mc.packet(0x00, mc.varint(774) + mc.string(host) + (25565).to_bytes(2, "big") + mc.varint(next_state))


async def read_json_packet(reader) -> tuple[int, dict]:
    _, pid, payload = await asyncio.wait_for(mc.read_packet(reader, 1 << 20), 15)
    text, _ = mc._read_string(payload, 0, 32767)
    return pid, json.loads(text)


async def server_list(port: int, host: str) -> str:
    """What the Minecraft server list shows for this address (the MOTD)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(handshake(host, mc.STATUS) + mc.packet(0x00, b""))
        await writer.drain()
        _, doc = await read_json_packet(reader)
        writer.write(mc.packet(0x01, b"pingpong"))
        await writer.drain()
        _, pid, payload = await asyncio.wait_for(mc.read_packet(reader, 64), 15)
        assert (pid, payload) == (0x01, b"pingpong")
        return doc["description"]["text"]
    finally:
        writer.close()


class Player:
    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer

    async def echo(self, data: bytes) -> bytes:
        self.writer.write(data)
        await self.writer.drain()
        return await asyncio.wait_for(self.reader.readexactly(len(data)), 15)

    async def closed(self, timeout: float = 20) -> bool:
        try:
            return await asyncio.wait_for(self.reader.read(1), timeout) == b""
        except (ConnectionError, OSError):
            return True

    def close(self) -> None:
        self.writer.close()


async def join(port: int, host: str, name: str, source: str | None = None, next_state: int = mc.LOGIN,
               waited: list | None = None) -> Player | str:
    """Log in: returns the connected player, or the refusal message shown on screen.
    ``source``: the local address to connect from (127.0.1.1 is another network than 127.0.0.1).
    Like Minecraft, answers "not understood" to the relay's login queries while it waits
    (counted in ``waited``)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port,
                                                   local_addr=(source, 0) if source else None)
    writer.write(handshake(host, next_state) + mc.packet(0x00, mc.string(name) + b"\x00" * 16))
    await writer.drain()
    while True:
        _, pid, payload = await asyncio.wait_for(mc.read_packet(reader, 1 << 20), 15)
        if pid != mc.LOGIN_PLUGIN_REQUEST:
            break
        message_id, _ = mc._read_varint(payload, 0)
        writer.write(mc.packet(mc.LOGIN_PLUGIN_RESPONSE, mc.varint(message_id) + b"\x00"))
        await writer.drain()
        if waited is not None:
            waited.append(message_id)
    if pid == 0x00:            # login disconnect
        writer.close()
        return json.loads(mc._read_string(payload, 0, 32767)[0])["text"]
    assert pid == 0x02 and mc._read_string(payload, 0, 16)[0] == name   # fake "login success"
    return Player(reader, writer)


# --------------------------------------------------------------------------- scenarios


async def two_pcs(tmp_path, backend, **server):
    oscar, lea = pc(tmp_path, backend, "pc_oscar"), pc(tmp_path, backend, "pc_lea")
    await oscar.api.register("oscar", PW, None)
    await oscar.login("oscar", PW)
    group = await oscar.api.create_group("Les copains")
    sid = (await oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3",
                                         memory_mb=1024, **server))["server_id"]
    code = (await oscar.api.create_invite(group["group_id"]))["code"]
    await lea.register("lea", PW, code)
    await oscar.api.set_priority(sid, oscar.settings.host_id, 100)
    await oscar.api.set_priority(sid, lea.settings.host_id, 70)
    await until(lambda: oscar.channel.connected and lea.channel.connected, what="both online")
    return oscar, lea, sid


def test_one_address_whatever_pc_hosts(tmp_path, backend) -> None:
    async def scenario():
        port = backend.relay_port
        oscar, lea, sid = await two_pcs(tmp_path, backend)
        s = await state(oscar, sid)
        assert s["public_address"] == f"mc.test:{port}"     # single server: the bare domain
        assert s["address_label"] == "survie"

        # Nobody hosts: the relay answers by itself, in French, instead of a timeout.
        assert "Serveur arrêté" in await server_list(port, "mc.test")
        assert "arrêté" in await join(port, "mc.test", "Alex")

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert await server_list(port, "mc.test") == "FAKE pc_oscar"          # Oscar's PC answers
        assert await server_list(port, "survie.mc.test") == "FAKE pc_oscar"   # label address too
        alex = await join(port, "mc.test", "Alex")
        assert isinstance(alex, Player)
        assert await alex.echo(b"\x05hello" * 2000) == b"\x05hello" * 2000    # bytes flow both ways
        await until(lambda: "Alex" in oscar.hosted.process.players, what="Alex seen by Minecraft")
        assert (await state(oscar, sid))["relay_connections"] == 1

        # Change of PC: the player is disconnected with the server, then the same address
        # leads to Léa's PC.
        await oscar.api.transfer(sid, lea.settings.host_id, countdown_s=0)
        assert await alex.closed()
        await until(lambda: lea.hosted is not None and lea.hosted.phase == "running", timeout=60,
                    what="Léa hosting")
        await wait_state(lea, sid, "running")
        assert await server_list(port, "mc.test") == "FAKE pc_lea"
        alex = await join(port, "mc.test", "Alex")
        assert isinstance(alex, Player) and await alex.echo(b"again") == b"again"
        alex.close()
        assert "Adresse inconnue" in await join(port, "nope.mc.test", "Alex")
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_unofficial_accounts_only_through_their_personal_address(tmp_path, backend, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario():
        port = backend.relay_port
        pack = {"name": "Mon pack", "url": "https://example.org/mon-pack.zip", "sha1": "d" * 40, "size": 99,
                "required": True}
        oscar, lea, sid = await two_pcs(tmp_path, backend, allow_offline_accounts=True,
                                        allowed_players=["Krystal", "Alex"],
                                        properties={"white-list": True}, resource_pack=pack)
        await lea.close()
        addresses = {a["player"]: a["address"] for a in await oscar.api.player_addresses(sid)}
        assert set(addresses) == {"Krystal", "Alex"}
        krystal_host = addresses["Krystal"].split(":")[0]
        assert krystal_host.endswith(".mc.test") and len(krystal_host.split(".")[0]) == 12
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")

        # The shared address is refused: anybody can claim any name without Microsoft.
        assert "adresse personnelle" in await join(port, "mc.test", "Krystal")
        assert "celle de Krystal" in await join(port, krystal_host, "Alex")     # someone else's
        krystal = await join(port, krystal_host, "krystal")                       # case-insensitive
        assert isinstance(krystal, Player) and await krystal.echo(b"hi") == b"hi"

        # The address leaked: the admin replaces it; the old one stops working at once.
        new = await oscar.api.regenerate_player_address(sid, "Krystal")
        assert new["address"] != addresses["Krystal"]
        assert await krystal.closed()
        assert "Adresse inconnue" in await join(port, krystal_host, "Krystal")
        assert isinstance(await join(port, new["address"].split(":")[0], "Krystal"), Player)

        # The admin prefers one address for everybody: the name is checked against the list, and
        # where the player connects from against the places already seen.
        await oscar.api.update_server(sid, personal_addresses=False)
        krystal = await join(port, "mc.test", "Krystal")                # first time: remembered
        assert isinstance(krystal, Player)
        krystal.close()
        assert "nouvel endroit" in await join(port, "mc.test", "Krystal", source="127.0.1.1")
        assert "Reconnecte-toi" in await join(port, "mc.test", "Bob")   # an access request
        requests = {(r["player"], r["kind"], r["place"]) for r in await oscar.api.access_requests(sid)}
        assert requests == {("Bob", "join", "127.0.0.0/24"), ("Krystal", "new_place", "127.0.1.0/24")}
        await oscar.api.answer_access_request(sid, "Krystal", True, "127.0.1.0/24")
        krystal = await join(port, "mc.test", "Krystal", source="127.0.1.1")
        assert isinstance(krystal, Player)
        krystal.close()
        personal = new["address"].split(":")[0]
        assert isinstance(await join(port, personal, "Krystal", source="127.0.2.1"), Player)  # no place check
        await oscar.api.update_server(sid, personal_addresses=True)
        assert "adresse personnelle" in await join(port, "mc.test", "Krystal")

        # Removed from the allowed players: the personal address disappears with them.
        await oscar.api.update_server(sid, allowed_players=["Krystal"])
        assert [a["player"] for a in await oscar.api.player_addresses(sid)] == ["Krystal"]
        assert "Adresse inconnue" in await join(port, addresses["Alex"].split(":")[0], "Alex")
        assert "relay refused" in caplog.text
        for address in (*addresses.values(), new["address"]):
            assert address.split(".")[0] not in caplog.text    # personal addresses are secrets
        props = (oscar.layout.server_dir(sid) / "server.properties").read_text("latin-1")
        assert "server-ip=127.0.0.1" in props                   # only reachable through the relay
        assert "resource-pack=https\\://example.org/mon-pack.zip" in props or \
            "resource-pack=https://example.org/mon-pack.zip" in props
        assert f"resource-pack-sha1={'d' * 40}" in props and "require-resource-pack=true" in props
        await oscar.close()

    asyncio.run(scenario())


def test_accepted_player_gets_their_personal_address_through_the_server_address(tmp_path, backend,
                                                                                  caplog) -> None:
    """0.8.1: the server's address is for the first connection; once accepted, the player sees
    their personal address there (from where they asked), so nobody types a name by hand."""
    caplog.set_level(logging.INFO)

    async def scenario():
        port = backend.relay_port
        oscar, lea, sid = await two_pcs(tmp_path, backend, allow_offline_accounts=True,
                                        allowed_players=["Krystal"], properties={"white-list": True})
        await lea.close()
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")

        assert "s'affichera" in await join(port, "mc.test", "Bob")          # the request
        assert "s'affichera" in await join(port, "mc.test", "Bob")          # not accepted yet
        await oscar.api.answer_access_request(sid, "Bob", True, "127.0.0.0/24")
        address = {a["player"]: a["address"] for a in await oscar.api.player_addresses(sid)}["Bob"]

        # From another network, the name alone shows nothing: the usual refusal.
        assert "demande-la" in await join(port, "mc.test", "Bob", source="127.0.1.1")
        # From where he asked: his personal address, on the screen.
        shown = await join(port, "mc.test", "Bob")
        assert "acceptée" in shown and address in shown
        assert "demande-la" in await join(port, "mc.test", "Krystal")         # added by hand: not shown
        bob = await join(port, address.split(":")[0], "Bob")
        assert isinstance(bob, Player) and await bob.echo(b"hi") == b"hi"
        bob.close()
        # Used once: the server's address stops showing it.
        assert "demande-la" in await join(port, "mc.test", "Bob")
        assert address.split(".")[0] not in caplog.text                       # never in the logs
        await oscar.close()

    asyncio.run(scenario())


def test_relay_cuts_a_host_whose_lease_ended(tmp_path, backend) -> None:
    """Barrier 4: a frozen host (no renewals) keeps its socket, but not its players."""
    async def scenario():
        port = backend.relay_port
        api = httpx.AsyncClient(base_url=backend.url + "/api/v1")
        await api.post("/auth/register", json={"username": "zed", "password": PW})
        token = (await api.post("/auth/login", json={"username": "zed", "password": PW})).json()["access_token"]
        api.headers["Authorization"] = f"Bearer {token}"
        gid = (await api.post("/groups", json={"name": "g"})).json()["group_id"]
        sid = (await api.post(f"/groups/{gid}/servers", json={"name": "s", "mc_version": "26.3",
                                                              "memory_mb": 1024})).json()["server_id"]
        hid = (await api.post("/hosts", json={"display_name": "pc", "os": "w", "cpu_cores": 8,
                                              "total_ram_mb": 32768})).json()["host_id"]

        async def echo(reader, writer):
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        local = await asyncio.start_server(echo, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        ws_base = backend.url.replace("http", "ws") + "/api/v1"

        async with websockets.connect(ws_base + "/ws") as control:
            await control.send(json.dumps({"type": "hello", "access_token": token, "host_id": hid,
                                           "client_version": __version__}))
            json.loads(await control.recv())
            availability = {"hosting_allowed": True, "free_ram_mb": 20000, "free_disk_mb": 200000, "relay_ok": True}
            seq = [0]

            async def beat(leases):
                seq[0] += 1
                await control.send(json.dumps({"type": "heartbeat", "seq": seq[0], "leases": leases,
                                               "availability": availability}))
            await beat([])
            await api.post(f"/servers/{sid}/start")
            offer = None
            while offer is None:
                msg = json.loads(await control.recv())
                offer = msg if msg["type"] == "hosting_offer" else None
            epoch = (await api.post(f"/servers/{sid}/lease/acquire",
                                    json={"host_id": hid, "offer_id": offer["offer_id"]})).json()["epoch"]
            await api.post(f"/servers/{sid}/host-state", json={"host_id": hid, "epoch": epoch, "state": "running"})

            async def answer_relay():
                tunnels = []
                async for raw in control:
                    msg = json.loads(raw)
                    if msg["type"] == "relay_open":
                        tunnels.append(asyncio.create_task(run_tunnel(
                            f"{ws_base}/relay/data/{msg['conn_id']}", msg["relay_token"], local_port)))
            answering = asyncio.create_task(answer_relay())

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(handshake("mc.test", mc.LOGIN) + mc.packet(0x00, mc.string("Zed") + b"\x00" * 16))
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(handshake("mc.test", mc.LOGIN))), 15)
            assert echoed == handshake("mc.test", mc.LOGIN)   # the host got the handshake replayed
            await asyncio.wait_for(reader.readexactly(len(mc.packet(0x00, mc.string("Zed") + b"\x00" * 16))), 15)
            player = Player(reader, writer)
            assert await player.echo(b"still here") == b"still here"

            # The host freezes: no renewal. Its lease (6 s here) expires and the relay cuts the
            # player off, although the host's data socket is still open.
            assert await player.closed(timeout=20)
            assert "a disparu" in await join(port, "mc.test", "Zed")   # orphaned: nobody to send to
            answering.cancel()
        local.close()
        await api.aclose()

    asyncio.run(scenario())


def test_relay_survives_garbage(tmp_path, backend) -> None:
    async def scenario():
        port = backend.relay_port
        for data in (b"\xfe\x01", b"\xff" * 10, mc.varint(4000) + b"x" * 4000, b"GE" + b"\x00" * 10):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(data)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 10) == b""   # dropped, nothing answered
            writer.close()
        # A browser gets a short "no status page here" (status pages are off by default).
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 10)).startswith(b"HTTP/1.1 404")
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)   # silent client
        assert await asyncio.wait_for(reader.read(), 10) == b""           # handshake timeout (5 s)
        assert "Adresse inconnue" in await server_list(port, "mc.test")    # still serving

    asyncio.run(scenario())

