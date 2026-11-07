"""Pure units of the Minecraft layer: logs, properties, JVM, resources."""

from pathlib import Path

import pytest

from mdh_client.minecraft import jvm, server_files
from mdh_client.minecraft.log_parser import LogParser
from mdh_client.minecraft.process import CommandRejected, validate_command
from mdh_client.monitoring.resources import max_allowed_memory_mb, suggest_memory_mb


@pytest.mark.parametrize("line, kind, value", [
    ('[12:00:01] [Server thread/INFO]: Done (3.456s)! For help, type "help"', "done", 3.456),
    ('[12:00:01 INFO]: Done (12,5s)! For help, type "help"', "done", 12.5),
    ("[12:00:02 INFO]: Steve joined the game", "joined", "Steve"),
    ("[12:00:02] [Server thread/INFO]: Alex_99 left the game", "left", "Alex_99"),
    ("[12:00:03 INFO]: There are 2 of a max of 20 players online: Steve, Alex", "list", ["Steve", "Alex"]),
    ("[12:00:03 INFO]: There are 0 of a max of 20 players online:", "list", []),
    ("[12:00:04 INFO]: Saved the game", "saved", None),
    ("[12:00:05 INFO]: TPS from last 1m, 5m, 15m: *20.0, 19.98, 19.97", "tps", 20.0),
    ("[12:00:05 INFO]: \x1b[0;32mTPS from last 1m, 5m, 15m: 18.5, 19.0, 19.9\x1b[m", "tps", 18.5),
    ("[12:00:06] [Server thread/INFO]: Average time per tick: 3.2ms (Target: 50.0ms)", "mspt", 3.2),
    ("[12:00:07 WARN]: You need to agree to the EULA in order to run the server.", "eula", None),
    ("[12:00:07 ERROR]: **** FAILED TO BIND TO PORT!", "bind_failed", None),
    # Minecraft 26.x: command feedback is logged as "System chat: ..." (seen on a real 26.3 server)
    ("[04:09:58] [Server thread/INFO]: System chat: Saved the game", "saved", None),
    ("[04:09:58] [Server thread/INFO]: System chat: There are 1 of a max of 20 players online: Krystal",
     "list", ["Krystal"]),
])
def test_log_events(line, kind, value) -> None:
    ev = LogParser().parse(line)
    assert ev is not None and ev.kind == kind
    if value is not None:
        assert ev.value == value


def test_paper_mspt_two_lines() -> None:
    p = LogParser()
    assert p.parse("[12:00:08 INFO]: Server tick times (avg/min/max) from last 5s, 10s, 1m:") is None
    ev = p.parse("[12:00:08 INFO]: ◴ 2.5/1.0/9.8, 2.6/1.0/9.8, 2.7/0.9/12.1")
    assert ev is not None and ev.kind == "mspt" and ev.value == 2.5


@pytest.mark.parametrize("line", [
    "[12:00:02 INFO]: <Eve> Oscar joined the game",
    "[12:00:02 INFO]: [Not Secure] <Eve> Oscar left the game",
    "[12:00:02 INFO]: <Eve> Done (1.0s)! For help, type",
    "[12:00:02 INFO]: Eve whispers: Saved the game",
    "[12:00:02 INFO]: <Eve> System chat: Saved the game",
])
def test_chat_cannot_spoof_events(line) -> None:
    ev = LogParser().parse(line)
    # At most what it is: a chat line by Eve (shown in the group's chat view), never an event.
    assert ev is None or (ev.kind == "chat" and ev.value[0] == "Eve")


def test_properties_merge_force_and_escape(tmp_path: Path) -> None:
    path = tmp_path / "server.properties"
    path.write_text("#comment\nlevel-name=world\nonline-mode=false\nenable-rcon=true\nmotd=old\n", "latin-1")
    merged = server_files.write_properties(path, {"motd": "Été\nonline-mode=false", "pvp": False,
                                                  "max-players": 12}, port=25570)
    text = path.read_text("latin-1")
    assert "online-mode=true\n" in text and "enable-rcon=false\n" in text
    assert "server-port=25570\n" in text and "level-name=world\n" in text
    assert "pvp=false\n" in text and "max-players=12\n" in text
    # Non-ASCII and control characters are escaped: no injected line.
    assert "motd=\\u00c9t\\u00e9\\u000aonline-mode=false\n" in text
    assert sum(1 for l in text.splitlines() if l.startswith("online-mode=")) == 1
    assert merged["online-mode"] == "true"


def test_offline_accounts_force_the_whitelist(tmp_path: Path) -> None:
    path = tmp_path / "server.properties"
    # The group cannot turn the whitelist off when identities are not verified.
    merged = server_files.write_properties(path, {"white-list": False}, port=25565,
                                           allow_offline_accounts=True)
    assert (merged["online-mode"], merged["white-list"], merged["enforce-whitelist"]) == ("false", "true", "true")
    assert merged["enable-rcon"] == "false"
    # Back to verified accounts: online-mode is forced again.
    merged = server_files.write_properties(path, {}, port=25565)
    assert merged["online-mode"] == "true"


def test_identities_reset_when_verification_mode_changes(tmp_path: Path) -> None:
    (tmp_path / "whitelist.json").write_text('[{"uuid": "x", "name": "oscar"}]', "utf-8")
    assert not server_files.reset_identities_if_auth_mode_changed(tmp_path, False)  # first time
    assert server_files.whitelisted_names(tmp_path) == {"oscar"}
    assert not server_files.reset_identities_if_auth_mode_changed(tmp_path, False)  # unchanged
    assert server_files.reset_identities_if_auth_mode_changed(tmp_path, True)       # switched
    assert server_files.whitelisted_names(tmp_path) == set()


def test_offline_uuid_matches_minecraft() -> None:
    # Known value: Java UUID.nameUUIDFromBytes("OfflinePlayer:Notch") as used by the server.
    assert server_files.offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"
    assert server_files.offline_uuid("Krystal")[14] == "3"  # version 3, never an official (v4) UUID


def test_offline_whitelist_replaces_official_uuids(tmp_path: Path) -> None:
    """Regression (real test): `whitelist add Krystal` stored Krystal's *official* UUID, so the
    TLauncher player (offline UUID) was refused although the name was whitelisted."""
    import json

    official = "143b508d-74b7-42a3-8c37-6c6762433c5f"
    (tmp_path / "whitelist.json").write_text(json.dumps([{"uuid": official, "name": "Krystal"}]), "utf-8")
    (tmp_path / "usercache.json").write_text(json.dumps([
        {"uuid": official, "name": "Krystal", "expiresOn": "2026-11-30 03:39:38 +0100"},
        {"uuid": "11111111-1111-4111-8111-111111111111", "name": "Other", "expiresOn": "x"}]), "utf-8")

    server_files.write_offline_whitelist(tmp_path, ["Krystal", "Lea_2"])

    whitelist = json.loads((tmp_path / "whitelist.json").read_text("utf-8"))
    assert whitelist == [{"uuid": server_files.offline_uuid("Krystal"), "name": "Krystal"},
                         {"uuid": server_files.offline_uuid("Lea_2"), "name": "Lea_2"}]
    cache = {e["name"]: e["uuid"] for e in json.loads((tmp_path / "usercache.json").read_text("utf-8"))}
    assert cache["Krystal"] == server_files.offline_uuid("Krystal")   # so `op Krystal` hits the right one
    assert cache["Other"] == "11111111-1111-4111-8111-111111111111"   # unrelated entries kept


def test_legacy_directory_counts_as_verified_mode(tmp_path: Path) -> None:
    (tmp_path / "whitelist.json").write_text("[]", "utf-8")
    assert server_files.reset_identities_if_auth_mode_changed(tmp_path, True)  # no marker = was online
    assert not (tmp_path / "whitelist.json").exists()


def test_properties_port_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        server_files.write_properties(tmp_path / "s.properties", {}, port=80)


def test_eula_requires_a_real_acceptance(tmp_path: Path) -> None:
    eula = tmp_path / "eula.txt"
    assert not server_files.eula_accepted(eula)
    with pytest.raises(ValueError):
        server_files.write_eula(eula, "yes")
    server_files.write_eula(eula, "2026-10-31T10:00:00+00:00")
    assert server_files.eula_accepted(eula)


def test_jvm_args() -> None:
    args = jvm.jvm_args(8192)
    assert args[:2] == ["-Xms8192M", "-Xmx8192M"]
    assert "-XX:G1HeapRegionSize=8M" in args
    assert "-XX:G1HeapRegionSize=16M" in jvm.jvm_args(16384)
    assert not any("OnOutOfMemoryError" in a or "OnError" in a for a in args)
    assert jvm.server_command("java.exe", "paper.jar", 4096)[-3:] == ["-jar", "paper.jar", "nogui"]
    with pytest.raises(ValueError):
        jvm.jvm_args(512)


@pytest.mark.parametrize("total, expected", [(32768, 8192), (16384, 4096), (8192, 2048), (65536, 8192),
                                             (4096, 1024)])
def test_memory_suggestion(total, expected) -> None:
    assert suggest_memory_mb(total) == expected


def test_memory_ceiling_keeps_room_for_windows() -> None:
    assert max_allowed_memory_mb(32768) == 21504   # keeps 1/3
    assert max_allowed_memory_mb(8192) == 4096     # keeps 4 GB


@pytest.mark.parametrize("cmd", ["", "x" * 300, "say hi\nop eve", "say \x00"])
def test_console_command_validation(cmd) -> None:
    with pytest.raises(CommandRejected):
        validate_command(cmd)
    assert validate_command("/whitelist add Steve") == "whitelist add Steve"
