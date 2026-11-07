"""0.11.0: anti-cheat protection — Grim from Modrinth, Paper's anti-X-ray turned on before each
start, the Fabric anti-X-ray mod, and the switch in the mods dialog."""

import asyncio
from types import SimpleNamespace

from PySide6.QtWidgets import QMessageBox

from fake_modrinth import FakeModrinth
from mdh_client.application.agent import HostAgent
from mdh_client.minecraft import anticheat
from mdh_client.minecraft.anticheat import MARK, configure_paper, edit_defaults
from mdh_client.minecraft.downloads import make_client
from mdh_client.minecraft.modpack import for_players
from mdh_client.minecraft.modrinth import Modrinth
from mdh_shared.models.api import anticheat_enabled, anticheat_slugs
from mdh_shared.models.enums import Loader
from test_gui import SyncBridge, _mods_agent, qapp  # noqa: F401
from test_world_handover import backend, state  # noqa: F401

GRIM, FAPI, AXR = "LJNGWSvH", "P7dR8mSH", "sml2FMaA"

#: The shape Paper writes (2-space indent, list items at their key's indent).
PAPER_FILE = """_version: 31
anticheat:
  anti-xray:
    enabled: false
    engine-mode: 1
    hidden-blocks:
    - copper_ore
    - diamond_ore
    lava-obscures: false
    max-block-height: 64
    replacement-blocks:
    - stone
    update-radius: 2
    use-permission: false
  obfuscation:
    items:
      hide-durability: false
chunks:
  auto-save-interval: default
"""


def _xray(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("  anti-xray:")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= 2), len(lines))
    return lines[start:end]


def test_paper_anti_xray_block_is_set_and_nothing_else_moves():
    on = edit_defaults(PAPER_FILE, True)
    block = _xray(on)
    assert f"    {MARK}" in block and "    enabled: true" in block and "    engine-mode: 2" in block
    assert "    - deepslate_diamond_ore" in block and "    - ancient_debris" in block and "    - air" not in block
    # Everything around the block is as Paper wrote it.
    assert on.startswith("_version: 31\nanticheat:\n  anti-xray:\n")
    assert on.endswith("  obfuscation:\n    items:\n      hide-durability: false\nchunks:\n  auto-save-interval: default\n")
    assert edit_defaults(on, True) == on                       # nothing to do the second time
    off = edit_defaults(on, False)
    assert _xray(off) == ["  anti-xray:", "    enabled: false"] and "obfuscation" in off
    # Set by hand (no mark): removing the protection leaves it alone.
    by_hand = PAPER_FILE.replace("enabled: false", "enabled: true")
    assert edit_defaults(by_hand, False) == by_hand
    assert edit_defaults(PAPER_FILE, False) == PAPER_FILE


def test_paper_file_without_the_block_or_missing(tmp_path):
    no_anticheat = "_version: 31\nchunks:\n  auto-save-interval: default\n"
    assert edit_defaults(no_anticheat, False) == no_anticheat
    added = edit_defaults(no_anticheat, True)
    assert added.startswith(no_anticheat) and "    enabled: true" in _xray(added)
    other_only = "anticheat:\n  obfuscation:\n    items: {}\nchunks: {}\n"
    inserted = edit_defaults(other_only, True)
    assert inserted.startswith("anticheat:\n  anti-xray:\n") and inserted.endswith("  obfuscation:\n    items: {}\nchunks: {}\n")

    # First start: the file does not exist yet; Paper fills in the rest.
    path = tmp_path / "config" / "paper-world-defaults.yml"
    assert configure_paper(tmp_path, "26.3", True) and "    enabled: true" in _xray(path.read_text("utf-8"))
    assert not configure_paper(tmp_path, "26.3", True)
    assert configure_paper(tmp_path, "26.3", False) and "    enabled: false" in _xray(path.read_text("utf-8"))
    # Before 1.19 Paper kept its settings elsewhere: nothing written.
    old = tmp_path / "old"
    assert not configure_paper(old, "1.18.2", True) and not old.exists()
    assert anticheat.paper_supported("1.21.11") and not anticheat.paper_supported("bad")


def modrinth() -> FakeModrinth:
    f = FakeModrinth()
    f.add(GRIM, "grimac", loaders=("paper", "spigot", "bukkit"), version_id="GrimPPPP", server_side="unknown",
          client_side="unknown", title="Grim Anticheat")
    f.add(FAPI, "fabric-api", version_id="FapiAAAA")
    f.add(GRIM, "grimac", loaders=("fabric",), version_id="GrimFFFF", deps=[FAPI], server_side="unknown",
          client_side="unknown")
    f.add(AXR, "anti-xray", version_id="AxrXXXXX", client_side="unsupported", title="AntiXray")
    return f


def test_what_the_protection_adds_per_server_type():
    assert anticheat_slugs(Loader.VANILLA) == ()
    fake = modrinth()

    async def added(loader):
        async with make_client(fake.transport()) as c:
            return await HostAgent.anticheat_mods(SimpleNamespace(modrinth=Modrinth(c)), loader, "26.3", [])

    paper = asyncio.run(added(Loader.PAPER))
    assert [(m.slug, m.version_id) for m in paper] == [("grimac", "GrimPPPP")] and anticheat_enabled(paper)
    fabric = asyncio.run(added(Loader.FABRIC))
    assert [m.slug for m in fabric] == ["grimac", "fabric-api", "anti-xray"]
    assert anticheat_enabled([m.model_dump(mode="json") for m in fabric]) and not anticheat_enabled([])

    # Players install nothing for it (Grim's page does not say it is for servers only).
    async def players():
        async with make_client(fake.transport()) as c:
            return await for_players(Modrinth(c), fabric)
    assert [p.mod.slug for p in asyncio.run(players())] == ["fabric-api"]


def test_the_switch_in_the_mods_dialog(qapp, monkeypatch):
    from mdh_client.gui.group_dialogs import ModsDialog

    fake = modrinth()
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    async def anticheat_mods(loader, mc_version, current):
        async with make_client(fake.transport()) as c:
            return await HostAgent.anticheat_mods(SimpleNamespace(modrinth=Modrinth(c)), loader, mc_version, current)

    agent = SimpleNamespace(anticheat_mods=anticheat_mods, **vars(_mods_agent(fake)))
    server = {"server_id": "a" * 32, "loader": "fabric", "mc_version": "26.3", "mods": [], "map_enabled": False}
    dlg = ModsDialog(SyncBridge(), agent, server, editable=True)
    assert not dlg.anticheat.isChecked() and dlg.anticheat.isEnabled()
    dlg.anticheat.click()
    assert "savoir quels mods un joueur a installés" in shown[-1]
    assert [m.slug for m in dlg.values()[0]] == ["grimac", "fabric-api", "anti-xray"] and "Grim" in dlg.info.text()
    dlg.anticheat.click()                                         # off: all three go
    assert dlg.values()[0] == []

    # Removing Grim from the list turns the switch off, and takes the anti-X-ray with it.
    dlg.anticheat.click()
    dlg.table.selectRow(0)
    dlg._remove()
    assert dlg.values()[0] == [] and not dlg.anticheat.isChecked()

    # Already on when the dialog opens; nothing found for that version: the switch goes back off.
    server["mods"] = [m.model_dump(mode="json") for m in asyncio.run(anticheat_mods(Loader.FABRIC, "26.3", []))]
    assert ModsDialog(SyncBridge(), agent, server, editable=True).anticheat.isChecked()
    old = ModsDialog(SyncBridge(), agent, {**server, "mc_version": "1.20.1", "mods": []}, editable=True)
    old.anticheat.click()
    assert not old.anticheat.isChecked() and old.error.text()

    paper = ModsDialog(SyncBridge(), agent, {**server, "loader": "paper", "mods": []}, editable=True)
    paper.anticheat.click()
    assert [m.slug for m in paper.values()[0]] == ["grimac"] and "anti-X-ray de Paper" in paper.info.text()
    vanilla = ModsDialog(SyncBridge(), agent, {**server, "loader": "vanilla", "mods": []}, editable=True)
    assert not vanilla.anticheat.isEnabled() and "Vanilla" in vanilla.anticheat.toolTip()
    viewer = ModsDialog(SyncBridge(), agent, server, editable=False)
    assert viewer.anticheat.isChecked() and not viewer.anticheat.isEnabled()


def test_a_paper_server_starts_with_grim_and_its_anti_xray(tmp_path, backend):  # noqa: F811
    from test_mods_live import pc, stop
    from test_agent_live import PW
    from test_relay_live import wait_state

    async def scenario():
        cdn = modrinth()
        oscar = pc(tmp_path, backend, "pc_oscar", cdn)
        await oscar.api.register("oscar", PW, None)
        await oscar.login("oscar", PW)
        group = await oscar.api.create_group("Les copains")
        sid = (await oscar.api.create_server(group["group_id"], name="Survie", mc_version="26.3", loader="paper",
                                             memory_mb=1024))["server_id"]
        await oscar.api.set_priority(sid, oscar.settings.host_id, 100)
        await oscar.save_mods(sid, await oscar.anticheat_mods(Loader.PAPER, "26.3", []))
        directory = oscar.layout.server_dir(sid)
        defaults = directory / "config" / "paper-world-defaults.yml"

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert [p.name for p in (directory / "plugins").glob("*.jar")] == ["grimac-GrimPPPP.jar"]
        assert "    enabled: true" in _xray(defaults.read_text("utf-8"))
        await stop(oscar, sid)

        await oscar.save_mods(sid, [])                       # protection off: Paper's anti-X-ray too
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert _xray(defaults.read_text("utf-8")) == ["  anti-xray:", "    enabled: false"]
        await stop(oscar, sid)
        await oscar.close()

    asyncio.run(scenario())
