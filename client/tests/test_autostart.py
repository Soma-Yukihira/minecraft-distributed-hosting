"""Start with Windows: one value in a per-user Run key (tested on a throwaway key)."""

import sys

import pytest

from mdh_client.system import autostart

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
TEST_KEY = r"Software\MinecraftDistributedHostingTests\Run"


@pytest.fixture
def key():
    import winreg
    yield TEST_KEY
    for path in (TEST_KEY, r"Software\MinecraftDistributedHostingTests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass


def test_enable_then_disable(key) -> None:
    import winreg
    exe = r"C:\Program Files\Minecraft Distributed Hosting\MinecraftDistributedHosting.exe"
    assert not autostart.is_enabled(key)
    autostart.set_enabled(True, executable=exe, key_path=key)
    assert autostart.is_enabled(key)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        value, kind = winreg.QueryValueEx(k, autostart.VALUE_NAME)
    assert kind == winreg.REG_SZ and value == f'"{exe}" --minimized'   # quoted: the path has spaces
    autostart.set_enabled(False, key_path=key)
    autostart.set_enabled(False, key_path=key)                           # idempotent
    assert not autostart.is_enabled(key)


def test_only_offered_by_the_installed_app(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not autostart.available()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert autostart.available()