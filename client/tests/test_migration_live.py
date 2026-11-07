"""Planned migrations between two real agents through a real control plane (Phase 5)."""

import asyncio

from test_agent_live import PW, Recorder, until
from test_world_handover import backend, pc, setup_two_pcs, state, wait_state, world_file  # noqa: F401


def migration_phases(rec: Recorder) -> list[str]:
    return [p["phase"] for n, p in rec.events if n == "migration"]


async def event_types(agent, sid) -> list[str]:
    return [e["type"] for e in reversed(await agent.api.events(sid, limit=200))]


def test_planned_transfer_moves_the_running_server(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, rec_l = await setup_two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.send_console("mark chest=diamonds")
        await oscar.backup_now()                 # Léa can prefetch this version in advance

        await oscar.api.transfer(sid, lea.settings.host_id, countdown_s=2)
        await until(lambda: lea.hosted is not None and lea.hosted.phase == "running", timeout=60,
                    what="Léa hosting")
        await wait_state(oscar, sid, "running")
        s = await state(oscar, sid)
        assert (s["active_host_id"], s["migration"]) == (lea.settings.host_id, None)
        assert oscar.hosted is None
        assert world_file(lea, sid, "chest") == "diamonds"   # the final world, not just the prefetch
        assert migration_phases(rec_l)[:2] == ["prefetching", "ready"]
        assert not lea._engine().prefetch_dir(lea.layout.server_dir(sid)).exists()

        commands = (oscar.layout.server_dir(sid) / "commands.log").read_text("utf-8").splitlines()
        assert any(c.startswith("say Le serveur change de PC dans 2 s") for c in commands)
        assert commands[-1] == "stop"
        events = await event_types(oscar, sid)
        assert events.index("migration_prefetched") < events.index("lease_released") \
            < events.index("migration_handover") < events.index("migration_completed")
        versions = await oscar.api.versions(sid)
        assert versions[0]["kind"] == "final" and versions[0]["version_id"] == s["head_version_id"]
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_cancel_during_countdown_keeps_playing_on_the_same_pc(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, rec_l = await setup_two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        pid = oscar.hosted.process.pid

        await oscar.api.transfer(sid, lea.settings.host_id, countdown_s=60)
        await until(lambda: "handing_over" in rec_o.phases(), timeout=30, what="countdown started")
        await lea.api.cancel_transfer(sid)
        await until(lambda: oscar.hosted.phase == "running" and not oscar.hosted.stopping, what="resumed")
        await wait_state(oscar, sid, "running")
        s = await state(oscar, sid)
        assert (s["active_host_id"], s["migration"]) == (oscar.settings.host_id, None)
        assert oscar.hosted.process.pid == pid             # Minecraft was never stopped
        commands = (oscar.layout.server_dir(sid) / "commands.log").read_text("utf-8")
        assert "say Changement de PC annulé : la partie continue." in commands
        assert lea.hosted is None
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_failed_final_upload_means_the_current_pc_carries_on(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, rec_l = await setup_two_pcs(tmp_path, backend)
        await oscar.api.start(sid)
        await wait_state(oscar, sid, "running")
        await oscar.send_console("mark chest=only-on-oscar")
        epoch = oscar.hosted.epoch

        async def upload_fails(h, label=None):
            return False                         # e.g. the upload link died during the handover
        oscar._final_backup = upload_fails
        await oscar.api.transfer(sid, lea.settings.host_id, countdown_s=0)
        await until(lambda: oscar.hosted is not None and oscar.hosted.phase == "running"
                    and oscar.hosted.migration_id is None and "handing_over" in rec_o.phases(),
                    timeout=60, what="Oscar hosting again")
        await wait_state(oscar, sid, "running")
        s = await state(oscar, sid)
        # Same lease, same PC: the world that exists only here was neither lost nor handed over.
        assert (s["active_host_id"], s["lease_epoch"], s["migration"]) == (oscar.settings.host_id, epoch, None)
        assert world_file(oscar, sid, "chest") == "only-on-oscar"
        assert lea.hosted is None
        assert "migration_cancelled" in await event_types(oscar, sid)
        del oscar._final_backup
        await oscar.close()
        await lea.close()

    asyncio.run(scenario())


def test_priority_pc_takes_the_server_back_when_it_returns(tmp_path, backend) -> None:
    async def scenario():
        oscar, lea, sid, rec_o, rec_l = await setup_two_pcs(tmp_path, backend)
        await oscar.close()                      # Oscar's PC (priority 100) is off
        await lea.api.start(sid)
        await wait_state(lea, sid, "running")
        await lea.send_console("mark chest=lea-was-here")

        oscar2 = pc(tmp_path, backend, "pc_oscar", Recorder())
        oscar2.settings.host_id = oscar.settings.host_id     # same PC (the app reloads its settings)
        await oscar2.login("oscar", PW)          # back online, but Steve is still playing on Léa's PC
        await asyncio.sleep(4)
        assert lea.hosted is not None and lea.hosted.phase == "running" and oscar2.hosted is None
        assert (await state(oscar2, sid))["migration"] is None

        await lea.send_console("leave")          # nobody online any more: "when_empty" kicks in
        await until(lambda: oscar2.hosted is not None and oscar2.hosted.phase == "running", timeout=60,
                    what="Oscar took the server back")
        s = await state(oscar2, sid)
        assert s["active_host_id"] == oscar2.settings.host_id
        assert world_file(oscar2, sid, "chest") == "lea-was-here"
        events = await oscar2.api.events(sid, limit=200)
        started = next(e for e in events if e["type"] == "migration_started")
        assert started["payload"]["reason"] == "preemption" and started["payload"]["countdown_s"] == 0
        await oscar2.close()
        await lea.close()

    asyncio.run(scenario())
