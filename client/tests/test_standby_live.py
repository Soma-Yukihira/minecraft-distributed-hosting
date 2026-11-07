"""Warm standby (docs/ARCHITECTURE.md 2.6): while one PC hosts, another keeps a copy of the latest
backup, so that when the host disappears without warning it takes over from local data."""

import asyncio
import sqlite3
import time

from test_agent_live import PW, Recorder, until
from test_world_handover import backend, pc, setup_two_pcs, wait_state, world_file  # noqa: F401


def pull_the_plug(agent) -> None:
    """Power cut on the hosting PC: nothing more is sent, Minecraft dies, no lease release."""
    h = agent.hosted
    for task in agent._tasks:
        task.cancel()
    if h.hot_task is not None:
        h.hot_task.cancel()
    agent._close_tunnels(h)
    h.process.kill()


def test_standby_pc_takes_over_from_its_own_copy(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar, lea, sid, _, rec_l = await setup_two_pcs(tmp_path, backend)
        oscar._hot_interval_override = 2.0        # backups every 2 s instead of every minute
        with sqlite3.connect(backend.db) as db:   # API minimum is 1 min: shortened for the test
            db.execute("UPDATE servers SET auto_failover_after_s = 1 WHERE id = ?", (sid,))

        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        assert oscar.hosted is not None
        await oscar.send_console("mark chest=diamonds")

        # Each backup Oscar's PC sends is copied on Léa's PC, without a lease, live world untouched.
        engine = lea._engine()
        live = lea.layout.server_dir(sid)

        async def copy_is_current():
            head = (await lea.api.server(sid))["head_version_id"]
            return head is not None and engine.prefetched_version(live) == head and world_copy() == "diamonds"

        def world_copy():
            path = engine.prefetch_dir(live) / "world" / "data" / "chest"
            return path.read_text("utf-8") if path.exists() else None

        await until(copy_is_current, timeout=30, what="standby copy")
        assert world_file(lea, sid, "chest") is None   # the copy stays aside until Léa hosts
        assert "standby" in rec_l.names()
        # The copy counts for the election, like a world already in place.
        assert lea._availability()["local_versions"][sid] == engine.prefetched_version(live)

        # The last backup before the power cut is copied too; from then on, any block Léa
        # fetches from the VPS is counted.
        oscar.hosted.hot_task.cancel()
        await oscar.send_console("mark sign=last")
        await oscar.backup(oscar.hosted)
        await until(copy_is_current, timeout=30, what="copy of the last backup")
        fetched = []
        real_get_blob = lea.api.get_blob

        async def counting_get_blob(group_id, sha):
            fetched.append(sha)
            return await real_get_blob(group_id, sha)

        lea.api.get_blob = counting_get_blob

        pull_the_plug(oscar)
        unplugged = time.monotonic()
        async def lea_runs_it():
            s = await lea.api.server(sid)
            return s["state"] == "running" and s["active_host_id"] == lea.settings.host_id

        await until(lea_runs_it, timeout=60, what="takeover by Léa's PC")
        took = time.monotonic() - unplugged
        assert world_file(lea, sid, "chest") == "diamonds" and world_file(lea, sid, "sign") == "last"
        assert fetched == []                             # nothing downloaded at takeover time
        assert not engine.prefetch_dir(live).exists()   # used up by the takeover
        # Lease (6 s) + grace (10 s) + relaunch delay (1 s) + start: well under a minute.
        assert took < 45, took

        oscar.hosted = None
        await oscar.api.close()
        await lea.close()

    asyncio.run(scenario())


def test_no_standby_copy_when_the_option_is_off(tmp_path, backend) -> None:  # noqa: F811
    async def scenario():
        oscar, lea, sid, _, rec_l = await setup_two_pcs(tmp_path, backend)
        lea.settings.standby_copy = False
        oscar._hot_interval_override = 2.0
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")

        async def hot_backup_done():
            return any(v["kind"] == "hot" for v in await oscar.api.versions(sid))

        await until(hot_backup_done, timeout=20, what="hot backup")
        await asyncio.sleep(2)
        assert "standby" not in rec_l.names()
        assert lea._engine().prefetched_version(lea.layout.server_dir(sid)) is None
        await lea.close()
        await oscar.close()

    asyncio.run(scenario())
