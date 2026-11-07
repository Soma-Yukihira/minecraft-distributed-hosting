"""WebSocket protocol hygiene."""

import json

from mdh_harness import WsConn


def raw_session(h) -> WsConn:
    conn = WsConn(h, None, None)  # type: ignore[arg-type]
    h.conns.append(conn)
    return conn


def test_hello_with_bad_token_is_refused(h) -> None:
    c = raw_session(h)
    c.send({"type": "hello", "access_token": "x" * 40, "client_version": "t"})
    assert c.expect_closed() == 1008


def test_first_frame_must_be_hello(h) -> None:
    c = raw_session(h)
    c.send({"type": "heartbeat", "seq": 1})
    assert c.expect_closed() == 1008


def test_cannot_claim_someone_elses_host(world) -> None:
    h = world.h
    c = raw_session(h)
    c.send({"type": "hello", "access_token": world.lea.access_token, "host_id": world.pc_oscar,
            "client_version": "t"})
    assert c.expect_closed() == 1008


def test_heartbeat_sequence_must_increase(world) -> None:
    h = world.h
    pc = h.connect_host(world.oscar, world.pc_oscar)
    pc.send({"type": "heartbeat", "seq": pc.seq})  # replayed sequence number
    assert pc.expect_closed() == 1008


def test_viewer_cannot_send_heartbeats(world) -> None:
    v = world.h.connect_viewer(world.lea)
    v.send({"type": "heartbeat", "seq": 1})
    assert v.expect_closed() == 1008


def test_unknown_messages_and_garbage_close_the_session(world) -> None:
    pc = world.h.connect_host(world.oscar, world.pc_oscar)
    pc.send({"type": "exec", "command": "op eve"})
    assert pc.expect_closed() == 1008
    pc2 = world.h.connect_host(world.oscar, world.pc_oscar)
    pc2.ws.send_text("not json")
    assert pc2.expect_closed() == 1008


def test_new_session_replaces_old_one_of_same_host(world) -> None:
    h = world.h
    old = h.connect_host(world.oscar, world.pc_oscar)
    new = h.connect_host(world.oscar, world.pc_oscar)
    assert old.expect_closed() == 4000
    assert new.heartbeat()["type"] == "heartbeat_ack"


def test_heartbeat_for_unknown_lease_is_refused_not_fatal(world) -> None:
    pc = world.h.connect_host(world.oscar, world.pc_oscar)
    ack = pc.heartbeat(leases={"0" * 32: 3, world.server_id: 1})
    assert [l["ok"] for l in ack["leases"]] == [False, False]


def test_oversized_frame_closes_session(world) -> None:
    pc = world.h.connect_host(world.oscar, world.pc_oscar)
    pc.ws.send_text(json.dumps({"type": "heartbeat", "seq": 99, "pad": "x" * 70_000}))
    assert pc.expect_closed() == 1008


def test_a_flood_of_messages_closes_the_session(harness_factory) -> None:
    h = harness_factory(ws_messages_per_minute=120.0, ws_messages_burst=30)
    oscar = h.register("oscar")
    pc = h.connect_host(oscar, h.register_host(oscar))
    for _ in range(40):  # far more than a heartbeat every 10 s
        pc.ws.send_text(json.dumps({"type": "heartbeat", "seq": pc.seq + 1}))
        pc.seq += 1
    codes = []
    try:
        while True:
            pc.recv(timeout=1.0)
    except Exception as exc:  # noqa: BLE001 - the session ends
        codes.append(getattr(exc, "code", None))
    assert codes == [1008]


def test_a_client_that_stops_reading_does_not_hold_up_the_others() -> None:
    """Without a timeout, one stuck send_text would block every broadcast (and the sweeper)."""
    import asyncio

    from mdh_server import realtime
    from mdh_shared.protocol.messages import GroupUpdate

    class Stuck:
        closed_with = None

        async def send_text(self, _):
            await asyncio.Event().wait()  # never returns

        async def close(self, code):
            self.closed_with = code

    class Fine:
        def __init__(self):
            self.got = []

        async def send_text(self, text):
            self.got.append(text)

    async def scenario():
        realtime.SEND_TIMEOUT_S, saved = 0.2, realtime.SEND_TIMEOUT_S
        try:
            hub = realtime.Hub()
            stuck, fine = Stuck(), Fine()
            s1 = realtime.Session("a" * 32, "u1", None, stuck)  # type: ignore[arg-type]
            s2 = realtime.Session("b" * 32, "u2", None, fine)  # type: ignore[arg-type]
            hub.register(s1)
            hub.register(s2)
            sent = await asyncio.wait_for(hub.broadcast(GroupUpdate(group_id="c" * 32)), 2.0)
            return sent, s1.closed, stuck.closed_with, len(fine.got)
        finally:
            realtime.SEND_TIMEOUT_S = saved

    assert asyncio.run(scenario()) == (1, True, 1011, 1)
