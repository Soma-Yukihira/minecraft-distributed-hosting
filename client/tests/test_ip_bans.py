"""IP bans on every type of server, enforced by the hosting PC (docs/ARCHITECTURE.md 4.22)."""

import json
import os

import pytest

from mdh_client.minecraft import ip_bans
from mdh_client.minecraft.process import CommandRejected


def write(path, ips) -> None:
    path.write_text(json.dumps([{"ip": ip, "created": "2026-11-08 10:00:00 +0100", "source": "Server",
                                 "expires": "forever", "reason": "Banned by an operator."} for ip in ips]), "utf-8")


def test_the_list_is_read_again_when_minecraft_rewrites_it(tmp_path) -> None:
    bans = ip_bans.BanList(tmp_path)
    assert not bans.bans("203.0.113.7")                       # no file yet
    write(tmp_path / "banned-ips.json", ["203.0.113.7", "2001:db8::1"])
    assert bans.bans("203.0.113.7") and bans.bans("::ffff:203.0.113.7") and bans.bans("2001:0db8::0001")
    assert not bans.bans("203.0.113.8") and not bans.bans(None) and not bans.bans("nonsense")
    write(tmp_path / "banned-ips.json", [])                     # pardon-ip
    os.utime(tmp_path / "banned-ips.json", ns=(1, 1))
    assert not bans.bans("203.0.113.7")
    (tmp_path / "banned-ips.json").write_text("{broken", "utf-8")
    os.utime(tmp_path / "banned-ips.json", ns=(2, 2))
    assert not bans.bans("203.0.113.7")


def test_a_ban_of_the_loopback_address_is_removed_before_a_start(tmp_path) -> None:
    write(tmp_path / "banned-ips.json", ["127.0.0.1", "203.0.113.7", "::1"])
    assert ip_bans.drop_loopback(tmp_path) == 2
    assert [e["ip"] for e in json.loads((tmp_path / "banned-ips.json").read_text("utf-8"))] == ["203.0.113.7"]
    assert ip_bans.drop_loopback(tmp_path) == 0
    assert ip_bans.drop_loopback(tmp_path / "missing") == 0


def test_ban_ip_by_name_uses_the_address_seen_by_the_relay() -> None:
    seen = {"alex": "203.0.113.7"}
    # Minecraft may only see 127.0.0.1 (Fabric, vanilla, or Paper behind an older VPS): the PC
    # always puts the real address in, then kicks the player.
    assert ip_bans.ban_commands("ban-ip Alex triche", seen) == [
        "ban-ip 203.0.113.7 triche", "kick Alex Tu es banni de ce serveur."]
    assert ip_bans.ban_commands("/ban-ip ALEX", seen)[0] == "ban-ip 203.0.113.7"
    assert ip_bans.ban_commands("minecraft:ban-ip Alex", seen)[0] == "ban-ip 203.0.113.7"   # full name
    assert ip_bans.ban_commands("/MINECRAFT:BAN-IP alex", seen)[0] == "ban-ip 203.0.113.7"
    # An address is kept as it is; other commands go through unchanged.
    assert ip_bans.ban_commands("ban-ip ::ffff:198.51.100.9", {}) == ["ban-ip 198.51.100.9"]
    assert ip_bans.ban_commands("say ban-ip Alex", seen) == ["say ban-ip Alex"]
    assert ip_bans.ban_commands("ban Alex", seen) == ["ban Alex"]
    assert ip_bans.ban_commands("execute run ban-ip 203.0.113.9", seen) == ["execute run ban-ip 203.0.113.9"]


def test_ban_ip_refusals() -> None:
    with pytest.raises(CommandRejected, match="inconnue"):
        ip_bans.ban_commands("ban-ip Steve", {})
    for loopback in ("127.0.0.1", "::1", "127.0.1.1"):
        with pytest.raises(CommandRejected, match="tout le monde"):
            ip_bans.ban_commands(f"ban-ip {loopback}", {})
    # Inside another command it cannot be translated, and would ban 127.0.0.1.
    for nested in ("execute as @a run ban-ip Alex", "execute run minecraft:ban-ip Alex", "execute run ban-ip ::1"):
        with pytest.raises(CommandRejected, match="tout le monde"):
            ip_bans.ban_commands(nested, {"alex": "203.0.113.7"})


def test_deban_ip_is_pardon_ip() -> None:
    seen = {"alex": "203.0.113.7"}
    assert ip_bans.ban_commands("deban-ip 203.0.113.7", {}) == ["pardon-ip 203.0.113.7"]
    assert ip_bans.ban_commands("/DEBAN-IP Alex", seen) == ["pardon-ip 203.0.113.7"]
    assert ip_bans.ban_commands("pardon-ip 203.0.113.7", {}) == ["pardon-ip 203.0.113.7"]
    with pytest.raises(CommandRejected, match="ni un joueur"):
        ip_bans.ban_commands("deban-ip Steve", seen)
