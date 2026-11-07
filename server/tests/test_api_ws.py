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
