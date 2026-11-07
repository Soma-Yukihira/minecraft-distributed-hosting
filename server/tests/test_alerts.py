"""Alerts of the VPS for group admins: disk, memory, quota, restart after a crash."""

from collections import namedtuple

import mdh_server.alerts as alerts
from mdh_server.alerts import RunMarker

Usage = namedtuple("Usage", "total used free")
GB = 1024**3


def test_only_admins_get_alerts_and_the_quota_one_is_per_group(h, monkeypatch) -> None:
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda path: Usage(100 * GB, 50 * GB, 50 * GB))
    oscar = h.register("oscar")
    gid = h.create_group(oscar, "Les copains")
    lea = h.register("lea", invite=h.invite(oscar, gid))
    assert h.get(oscar, "/alerts").json() == []

    h.ctx.conn.execute("UPDATE groups SET storage_quota_bytes = ? WHERE id = ?", (10 * GB, gid))
    h.ctx.conn.execute("INSERT INTO blobs VALUES (?, 'a', 1, ?, 0)", (gid, int(8.5 * GB)))
    found = h.get(oscar, "/alerts").json()
    assert [(a["code"], a["severity"], a["group_id"]) for a in found] == [("group_quota_high", "warning", gid)]
    assert found[0]["details"]["group_name"] == "Les copains"
    assert h.get(lea, "/alerts").json() == []                     # a member can do nothing about it

    h.ctx.conn.execute("UPDATE blobs SET stored_size = ? WHERE group_id = ?", (int(9.7 * GB), gid))
    assert h.get(oscar, "/alerts").json()[0]["severity"] == "critical"


def test_disk_of_the_vps(h, monkeypatch) -> None:
    oscar = h.register("oscar")
    h.create_group(oscar)
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda path: Usage(80 * GB, 73 * GB, 7 * GB))
    assert [(a["code"], a["severity"]) for a in h.get(oscar, "/alerts").json()] == [("vps_disk_low", "warning")]
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda path: Usage(80 * GB, 78 * GB, 1 * GB))
    assert h.get(oscar, "/alerts").json()[0]["severity"] == "critical"


def test_restart_after_a_crash_is_noticed(tmp_path) -> None:
    clean = RunMarker(tmp_path)
    clean.started(1000.0)
    clean.stopped()
    again = RunMarker(tmp_path)
    again.started(2000.0)
    assert again.unclean_start_at is None                         # stopped cleanly before

    crashed = RunMarker(tmp_path)                                 # "again" never called stopped()
    crashed.started(3000.0)
    assert crashed.unclean_start_at == 3000.0
    codes = [a.code for a in alerts.vps_alerts(tmp_path, crashed, 3000.0 + 3600)]
    assert "vps_restarted" in codes
    assert "vps_restarted" not in [a.code for a in alerts.vps_alerts(tmp_path, crashed, 3000.0 + 2 * 86400)]
