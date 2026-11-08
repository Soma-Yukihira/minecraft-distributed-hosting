"""proxies.proxy-protocol in Paper's paper-global.yml (docs/ARCHITECTURE.md 4.22)."""

from mdh_client.minecraft import proxy_protocol
from mdh_shared.models.enums import Loader

PAPER = """_version: 29
chunk-loading-basic:
  autoconfig-send-distance: true
proxies:
  bungee-cord:
    online-mode: true
  proxy-protocol: false
  velocity:
    enabled: false
    secret: ''
scoreboards:
  save-empty-scoreboard-teams: true
"""


def test_only_the_setting_changes() -> None:
    new = proxy_protocol.edit_global(PAPER)
    assert new == PAPER.replace("  proxy-protocol: false", "  proxy-protocol: true")
    assert proxy_protocol.edit_global(new) == new                     # nothing more to do


def test_missing_key_or_block_or_file() -> None:
    without_key = PAPER.replace("  proxy-protocol: false\n", "")
    assert "proxies:\n  proxy-protocol: true\n  bungee-cord:" in proxy_protocol.edit_global(without_key)
    without_block = "_version: 29\nscoreboards:\n  save-empty-scoreboard-teams: true"
    assert proxy_protocol.edit_global(without_block).endswith("true\nproxies:\n  proxy-protocol: true\n")
    assert proxy_protocol.edit_global("") == "proxies:\n  proxy-protocol: true\n"


def test_a_nested_key_of_the_same_name_is_not_this_one() -> None:
    text = "proxies:\n  velocity:\n    proxy-protocol: false\n"
    assert proxy_protocol.edit_global(text) == "proxies:\n  proxy-protocol: true\n  velocity:\n    proxy-protocol: false\n"


def test_which_servers(tmp_path) -> None:
    assert proxy_protocol.configure(tmp_path, Loader.PAPER, "26.3") is True
    assert (tmp_path / "config" / "paper-global.yml").read_text("utf-8") == "proxies:\n  proxy-protocol: true\n"
    for loader, version in ((Loader.PAPER, "1.18.2"), (Loader.FABRIC, "26.3"), (Loader.VANILLA, "26.3")):
        other = tmp_path / f"{loader}-{version}"
        assert proxy_protocol.configure(other, loader, version) is False
        assert not (other / "config").exists()
