"""Minecraft process management against a fake server; Windows Job Object guarantees."""

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from mdh_client.credentials import protect, unprotect
from mdh_client.minecraft.job import CREATE_SUSPENDED, JobObject
from mdh_client.minecraft.process import MinecraftProcess, ProcessState
from mdh_client.minecraft.server_files import write_eula

FAKE = Path(__file__).with_name("fake_minecraft.py")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only feature")


def server_dir(tmp_path: Path, eula: bool = True) -> Path:
    d = tmp_path / "srv"
    d.mkdir()
    if eula:
        write_eula(d / "eula.txt", "2026-10-31T10:00:00+00:00")
    return d


def run(coro):
    return asyncio.run(coro)


def test_start_console_and_graceful_stop(tmp_path: Path) -> None:
    d = server_dir(tmp_path)
    states: list[str] = []

    async def go():
        p = MinecraftProcess([sys.executable, "-u", str(FAKE)], d, on_state=lambda s: states.append(s.value))
        await p.start()
        assert await p.wait_ready(10)
        assert p.players == {"Steve"}
        await p.send("list")
        assert await p.save_all_flush(5)
        await p.send("tps")
        await asyncio.sleep(0.3)
        assert p.tps == 19.9
        assert await p.stop(10) == 0

    run(go())
    assert states == ["starting", "running", "stopping", "stopped"]
    assert (d / "commands.log").read_text().splitlines() == ["list", "save-all flush", "tps", "stop"]


def test_crash_is_detected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MC_MODE", "crash")
    d = server_dir(tmp_path)

    async def go():
        p = MinecraftProcess([sys.executable, "-u", str(FAKE)], d)
        await p.start()
        assert await p.wait_ready(10)
        assert await asyncio.wait_for(p.wait_exit(), 10) == 1
        return p

    p = run(go())
    assert p.state == ProcessState.CRASHED
    assert any("Exception in server tick loop" in line for line in p.lines)


def test_eula_missing_is_reported(tmp_path: Path) -> None:
    d = server_dir(tmp_path, eula=False)

    async def go():
        p = MinecraftProcess([sys.executable, "-u", str(FAKE)], d)
        await p.start()
        assert not await p.wait_ready(10)
        return p

    p = run(go())
    assert p.eula_required and p.state == ProcessState.CRASHED


def test_hung_server_is_killed_on_stop_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MC_MODE", "hang")
    d = server_dir(tmp_path)

    async def go():
        p = MinecraftProcess([sys.executable, "-u", str(FAKE)], d)
        await p.start()
        assert not await p.wait_ready(0.5)
        t = time.monotonic()
        await p.stop(timeout=1)
        assert time.monotonic() - t < 5
        assert not p.alive

    run(go())


@windows_only
def test_job_object_kills_whole_process_tree_when_closed() -> None:
    # sys.executable is the venv launcher, which immediately spawns the real interpreter: the
    # suspended start must keep that grandchild inside the job too.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=CREATE_SUSPENDED)
    job = JobObject()
    job.assign_and_resume(child.pid)
    deadline = time.monotonic() + 5
    tree: list[psutil.Process] = []
    while time.monotonic() < deadline:
        tree = [psutil.Process(child.pid), *psutil.Process(child.pid).children(recursive=True)]
        if len(tree) >= 2 or "python" in tree[0].name().lower():
            break
        time.sleep(0.05)
    job.close()
    gone, alive = psutil.wait_procs(tree, timeout=5)
    assert not alive


@windows_only
def test_minecraft_dies_with_a_killed_agent(tmp_path: Path) -> None:
    """P3: if the agent process is killed brutally, Minecraft must not survive it."""
    d = server_dir(tmp_path)
    pid_file = tmp_path / "mc.pid"
    agent_script = textwrap.dedent(f"""
        import asyncio, sys
        sys.path[:0] = {[str(Path(__file__).parents[1]), str(Path(__file__).parents[2] / "shared")]!r}
        from pathlib import Path
        from mdh_client.minecraft.process import MinecraftProcess
        async def main():
            p = MinecraftProcess([sys.executable, "-u", {str(FAKE)!r}], Path({str(d)!r}))
            await p.start()
            await p.wait_ready(10)
            Path({str(pid_file)!r}).write_text(str(p.pid))
            await asyncio.sleep(60)
        asyncio.run(main())
    """)
    agent = subprocess.Popen([sys.executable, "-c", agent_script])
    deadline = time.monotonic() + 20
    # The file can exist before its content is written: wait for a complete PID.
    while time.monotonic() < deadline and not (pid_file.exists() and pid_file.read_text().strip().isdigit()):
        time.sleep(0.1)
    mc = psutil.Process(int(pid_file.read_text()))
    tree = [mc, *mc.children(recursive=True)]  # launcher + real interpreter, or java.exe alone
    assert all(p.is_running() for p in tree)
    agent.kill()                       # like a crash or the Task Manager "End task"
    agent.wait(timeout=5)
    gone, alive = psutil.wait_procs(tree, timeout=5)
    assert not alive, f"survivors: {alive}"


@windows_only
def test_cpu_cap_job_can_be_created() -> None:
    job = JobObject(cpu_limit_percent=50)
    job.close()
    with pytest.raises(ValueError):
        JobObject(cpu_limit_percent=1)


@windows_only
def test_dpapi_roundtrip() -> None:
    secret = os.urandom(32)
    blob = protect(secret)
    assert blob != secret and secret not in blob
    assert unprotect(blob) == secret
    with pytest.raises(OSError):
        unprotect(blob[:-4] + b"xxxx")
