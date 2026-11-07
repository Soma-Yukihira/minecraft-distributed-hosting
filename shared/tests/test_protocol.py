import json
import uuid

import pytest
from pydantic import ValidationError

from mdh_shared.protocol.messages import (
    ActionMessage,
    Heartbeat,
    HeartbeatAck,
    LeaseRef,
    LeaseStatus,
    MessageTooLarge,
    encode,
    parse_client_message,
    parse_server_message,
)


def new_id() -> str:
    return uuid.uuid4().hex


def test_heartbeat_round_trip() -> None:
    hb = Heartbeat(seq=3, leases=(LeaseRef(server_id=new_id(), epoch=7),))
    assert parse_client_message(encode(hb)) == hb


def test_server_message_round_trip() -> None:
    ack = HeartbeatAck(seq=3, leases=(LeaseStatus(server_id=new_id(), epoch=7, ok=False),))
    assert parse_server_message(encode(ack)) == ack


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "exec", "command": "op attacker"},
        {"type": "heartbeat", "seq": 1, "unexpected": True},
        {"type": "heartbeat", "seq": -1},
        {"type": "offer_reply", "offer_id": "not-an-id", "accept": True},
    ],
)
def test_rejects_unknown_or_malformed_client_messages(payload: dict) -> None:
    with pytest.raises(ValidationError):
        parse_client_message(json.dumps(payload))


def test_rejects_oversized_frames() -> None:
    with pytest.raises(MessageTooLarge):
        parse_client_message("x" * (64 * 1024 + 1))


def test_actions_are_a_closed_list() -> None:
    ok = {"type": "action", "server_id": new_id(), "action_id": new_id(),
          "action": {"kind": "whitelist_add", "player": "Notch_1"}}
    assert isinstance(parse_server_message(json.dumps(ok)), ActionMessage)

    for action in (
        {"kind": "command", "command": "stop"},
        {"kind": "whitelist_add", "player": "bad name; op me"},
        {"kind": "op", "player": "x"},
    ):
        bad = {**ok, "action": action}
        with pytest.raises(ValidationError):
            parse_server_message(json.dumps(bad))
