"""World storage on the VPS: blocks, versions (fencing + compare-and-swap), restore, retention."""

import hashlib
import os

import pytest
import zstandard

from mdh_server.storage.retention import RetentionPolicy, VersionRow, collect, versions_to_keep
from mdh_shared.constants import BLOCK_SIZE
from mdh_shared.models.manifest import FileEntry, Manifest
from test_api_coordination import crash_oscar, start_on_oscar


def block(data: bytes) -> tuple[str, bytes]:
    return hashlib.sha256(data).hexdigest(), zstandard.ZstdCompressor().compress(data)


def manifest_of(files: dict[str, bytes]) -> Manifest:
    entries = []
    for path in sorted(files):
        data = files[path]
        blocks = tuple(hashlib.sha256(data[i:i + BLOCK_SIZE]).hexdigest() for i in range(0, len(data), BLOCK_SIZE))
        entries.append(FileEntry(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), blocks=blocks))
    return Manifest(files=tuple(entries))


def upload(world, pc, files: dict[str, bytes]) -> None:
    h, sid = world.h, world.server_id
    for data in files.values():
        for i in range(0, len(data), BLOCK_SIZE):
            sha, z = block(data[i:i + BLOCK_SIZE])
            r = h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{sha}", headers=world.oscar.headers,
                             content=z, params={"server_id": sid, "host_id": pc.host_id,
                                                "epoch": pc.leases[sid]})
            assert r.status_code == 204, r.text


def commit(world, pc, files, parent, kind="final"):
    return world.h.post(world.oscar, f"/servers/{world.server_id}/versions", {
        "host_id": pc.host_id, "epoch": pc.leases[world.server_id], "parent_version_id": parent,
        "kind": kind, "manifest": manifest_of(files).model_dump(mode="json")})


WORLD = {"world/level.dat": b"level-1", "world/region/r.0.0.mca": os.urandom(BLOCK_SIZE + 1000)}


def test_upload_commit_and_read_back(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    hashes = sorted(manifest_of(WORLD).block_hashes())
    r = h.post(world.oscar, f"/groups/{world.group_id}/blobs/missing", {"hashes": hashes})
    assert sorted(r.json()["missing"]) == hashes
    upload(world, pc_o, WORLD)
    assert h.post(world.oscar, f"/groups/{world.group_id}/blobs/missing", {"hashes": hashes}).json()["missing"] == []

    r = commit(world, pc_o, WORLD, None, kind="hot")
    assert r.status_code == 201, r.text
    v1 = r.json()
    status = h.status(world.oscar, world.server_id)
    assert status["head_version_id"] == v1["version_id"] and status["last_backup_at"] is not None

    manifest = h.get(world.oscar, f"/servers/{world.server_id}/versions/{v1['version_id']}/manifest").json()
    assert Manifest.model_validate(manifest) == manifest_of(WORLD)
    sha, z = block(b"level-1")
    got = h.get(world.oscar, f"/groups/{world.group_id}/blobs/{sha}")
    assert got.status_code == 200 and zstandard.ZstdDecompressor().decompress(got.content) == b"level-1"

    # Incremental: only the changed block is new; the commit chains on the previous head.
    world2 = {**WORLD, "world/level.dat": b"level-2"}
    missing = h.post(world.oscar, f"/groups/{world.group_id}/blobs/missing",
                     {"hashes": sorted(manifest_of(world2).block_hashes())}).json()["missing"]
    assert missing == [hashlib.sha256(b"level-2").hexdigest()]
    upload(world, pc_o, {"world/level.dat": b"level-2"})
    v2 = commit(world, pc_o, world2, v1["version_id"]).json()
    assert v2["parent_version_id"] == v1["version_id"] and v2["kind"] == "final"
    usage = h.get(world.oscar, f"/groups/{world.group_id}/storage").json()
    assert usage["servers"][0]["versions"] == 2 and usage["used_bytes"] > BLOCK_SIZE // 2


def test_commit_requires_all_blocks_and_current_head(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    r = commit(world, pc_o, WORLD, None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_blobs"
    upload(world, pc_o, WORLD)
    v1 = commit(world, pc_o, WORLD, None).json()
    r = commit(world, pc_o, WORLD, None)              # built on an outdated head
    assert r.status_code == 409 and r.json()["error"]["code"] == "parent_mismatch"
    assert h.status(world.oscar, world.server_id)["head_version_id"] == v1["version_id"]


@pytest.mark.parametrize("payload, code, status", [
    (zstandard.ZstdCompressor().compress(b"other content"), "hash_mismatch", 422),
    (b"not zstd at all", "invalid_block", 422),
    (zstandard.ZstdCompressor().compress(b"\0" * (BLOCK_SIZE * 50)), "invalid_block", 422),  # bomb
    (os.urandom(BLOCK_SIZE + 70 * 1024), "too_large", 413),
])
def test_bad_blocks_are_rejected(world, payload, code, status) -> None:
    pc_o, _, _ = start_on_oscar(world)
    sha = hashlib.sha256(b"announced").hexdigest()
    r = world.h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{sha}", headers=world.oscar.headers,
                           content=payload, params={"server_id": world.server_id, "host_id": pc_o.host_id,
                                                    "epoch": pc_o.leases[world.server_id]})
    assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert not list((world.h.ctx.store.root).rglob("*.zst"))


def test_only_the_lease_holder_can_write(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    sha, z = block(b"data")
    r = h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{sha}", headers=world.lea.headers, content=z,
                     params={"server_id": world.server_id, "host_id": world.pc_lea, "epoch": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_epoch"
    r = h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{sha}", headers=world.lea.headers, content=z,
                     params={"server_id": world.server_id, "host_id": world.pc_oscar, "epoch": 1})
    assert r.status_code == 404  # not her machine


def test_zombie_host_cannot_commit_after_takeover(world) -> None:
    h = world.h
    pc_o, pc_l, _ = start_on_oscar(world)
    upload(world, pc_o, WORLD)
    crash_oscar(world, pc_o)
    h.advance(15)
    h.post(world.oscar, f"/servers/{world.server_id}/failover/confirm")
    pc_l.accept_next_offer()
    r = commit(world, pc_o, WORLD, None)    # Oscar's PC comes back with epoch 1
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_epoch"
    assert h.status(world.oscar, world.server_id)["head_version_id"] is None


def test_quota_is_enforced(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    h.ctx.conn.execute("UPDATE groups SET storage_quota_bytes = 100 WHERE id = ?", (world.group_id,))
    sha, z = block(os.urandom(5000))
    r = h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{sha}", headers=world.oscar.headers, content=z,
                     params={"server_id": world.server_id, "host_id": pc_o.host_id, "epoch": 1})
    assert (r.status_code, r.json()["error"]["code"]) == (507, "quota_exceeded")


def test_world_data_access_rights(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    upload(world, pc_o, WORLD)
    sha = hashlib.sha256(b"level-1").hexdigest()
    eve = h.register("eve")
    assert h.get(eve, f"/groups/{world.group_id}/blobs/{sha}").status_code == 404
    h.client.patch(f"/api/v1/groups/{world.group_id}/members/{world.lea.user_id}", headers=world.oscar.headers,
                   json={"can_host": False})
    assert h.get(world.lea, f"/groups/{world.group_id}/blobs/{sha}").status_code == 403


def test_restore_creates_a_new_head_and_needs_a_stopped_server(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    upload(world, pc_o, WORLD)
    v1 = commit(world, pc_o, WORLD, None).json()
    world2 = {**WORLD, "world/level.dat": b"level-2"}
    upload(world, pc_o, world2)
    v2 = commit(world, pc_o, world2, v1["version_id"]).json()
    r = h.post(world.oscar, f"/servers/{world.server_id}/restore", {"version_id": v1["version_id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_running"

    h.post(world.oscar, f"/servers/{world.server_id}/stop", {"countdown_s": 0})
    pc_o.release(world.server_id)
    assert h.post(world.lea, f"/servers/{world.server_id}/restore",
                  {"version_id": v1["version_id"]}).status_code == 403
    r = h.post(world.oscar, f"/servers/{world.server_id}/restore", {"version_id": v1["version_id"]})
    assert r.status_code == 201
    restored = r.json()
    assert restored["kind"] == "restore" and restored["parent_version_id"] == v2["version_id"]
    assert restored["manifest_sha256"] == v1["manifest_sha256"]
    versions = h.get(world.oscar, f"/servers/{world.server_id}/versions").json()
    assert [v["version_id"] for v in versions][:3] == [restored["version_id"], v2["version_id"], v1["version_id"]]


def test_pin_is_admin_only(world) -> None:
    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    upload(world, pc_o, WORLD)
    v1 = commit(world, pc_o, WORLD, None).json()
    path = f"/servers/{world.server_id}/versions/{v1['version_id']}/pin"
    assert h.post(world.lea, path, {"pinned": True}).status_code == 403
    r = h.post(world.oscar, path, {"pinned": True, "label": "avant le dragon"})
    assert r.json()["pinned"] is True and r.json()["label"] == "avant le dragon"


# ----------------------------------------------------------------------------- retention

DAY = 86400.0


def test_retention_policy() -> None:
    now = 100 * DAY
    policy = RetentionPolicy(keep_last=3, hourly_hours=24, daily_days=7)
    rows = [VersionRow(f"m{i}", now - i * 600, False) for i in range(12)]         # every 10 min, 2 h
    rows += [VersionRow(f"d{i}", now - i * DAY - 3600, False) for i in range(1, 10)]  # one per day
    rows += [VersionRow("old-pinned", now - 60 * DAY, True)]
    keep = versions_to_keep(rows, "d9", now, policy)
    assert {"m0", "m1", "m2"} <= keep                       # last 3
    assert "old-pinned" in keep and "d9" in keep            # pinned and head always
    assert {"d1", "d2", "d3", "d4", "d5", "d6"} <= keep     # one per day over 7 days
    assert "d8" not in keep                                  # older than 7 days
    hourly = {r.version_id for r in rows if r.version_id.startswith("m")} & keep
    assert len(hourly) <= 3 + 3                              # last 3 + newest of each of the 2-3 hours


def test_collect_deletes_old_versions_and_orphan_blocks_only(world) -> None:
    import uuid

    h = world.h
    pc_o, _, _ = start_on_oscar(world)
    for i in range(8):
        upload(world, pc_o, {"world/level.dat": f"level-{i}".encode()})
    # History spread over 8 days (written directly: sessions and leases do not last that long).
    conn, start, parent = h.ctx.conn, h.clock(), None
    for i in range(8):
        manifest = manifest_of({"world/level.dat": f"level-{i}".encode()})
        digest = h.ctx.store.put_manifest(world.group_id, manifest)
        vid = uuid.uuid4().hex
        conn.execute("INSERT INTO world_versions (id, server_id, parent_id, epoch, host_id, kind, manifest_sha256,"
                     " total_size, created_at) VALUES (?, ?, ?, 1, ?, 'final', ?, 7, ?)",
                     (vid, world.server_id, parent, pc_o.host_id, digest, start + i * DAY))
        conn.executemany("INSERT INTO version_blobs VALUES (?, ?)", [(vid, b) for b in manifest.block_hashes()])
        conn.execute("UPDATE servers SET head_version_id = ? WHERE id = ?", (vid, world.server_id))
        parent = vid
    h.advance(8 * DAY)
    oscar = h.login("oscar")
    pc_o.leases[world.server_id] = 1
    fresh_sha, z = block(b"uploaded but not committed yet")
    conn.execute("UPDATE leases SET expires_at = ? WHERE server_id = ?", (h.clock() + 30, world.server_id))
    r = h.client.put(f"/api/v1/groups/{world.group_id}/blobs/{fresh_sha}", headers=oscar.headers, content=z,
                     params={"server_id": world.server_id, "host_id": pc_o.host_id, "epoch": 1})
    assert r.status_code == 204, r.text

    report = collect(h.ctx.conn, h.ctx.store, h.clock(), RetentionPolicy(keep_last=2, hourly_hours=1, daily_days=3))
    remaining = h.get(oscar, f"/servers/{world.server_id}/versions").json()
    assert report.versions_deleted == 8 - len(remaining) and len(remaining) < 8
    assert remaining[0]["version_id"] == parent                      # head kept
    assert report.blobs_deleted >= 1
    kept_hashes = {hashlib.sha256(f"level-{i}".encode()).hexdigest() for i in range(8)}
    on_disk = {p.name.removesuffix(".zst") for p in h.ctx.store.root.rglob("*.zst")
               if "blobs" in p.parts}
    assert fresh_sha in on_disk                                       # in-flight upload protected
    assert hashlib.sha256(b"level-7").hexdigest() in on_disk          # head's block kept
    assert hashlib.sha256(b"level-0").hexdigest() not in on_disk      # old, unreferenced: gone
    assert on_disk - {fresh_sha} <= kept_hashes
    # The kept versions are still fully readable.
    for v in remaining:
        assert h.get(oscar, f"/servers/{world.server_id}/versions/{v['version_id']}/manifest").status_code == 200
    # Children of deleted versions lost their parent pointer instead of blocking the deletion.
    assert all(v["parent_version_id"] in {None, *(x["version_id"] for x in remaining)} for v in remaining)


def test_a_big_world_is_not_throttled_like_ordinary_requests(world) -> None:
    """Found by the load test: one request per block hit the general per-user limit (120 burst),
    so a world of a few hundred MB could never be uploaded."""
    pc_o, _, _ = start_on_oscar(world)
    files = {f"world/region/r.{i}.0.mca": f"chunk-{i}".encode() for i in range(400)}
    upload(world, pc_o, files)                                  # 400 block uploads in a row
    assert commit(world, pc_o, files, None).status_code == 201
    assert world.h.get(world.oscar, f"/servers/{world.server_id}").status_code == 200

def test_new_world_keeps_settings_drops_the_world_and_pins_the_old_one(world) -> None:
    h, sid = world.h, world.server_id
    pc_o, _, _ = start_on_oscar(world)
    files = {**WORLD, "world_nether/DIM-1/region/r.0.0.mca": b"nether", "ops.json": b"[]",
             "config/mod.toml": b"x=1", "server.properties": b"level-seed=1"}
    upload(world, pc_o, files)
    old = commit(world, pc_o, files, None).json()
    body = {"seed": "Les copains", "level_type": "large_biomes", "generate_structures": False}
    r = h.post(world.oscar, f"/servers/{sid}/new-world", body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_running"

    h.post(world.oscar, f"/servers/{sid}/stop", {"countdown_s": 0})
    pc_o.release(sid)
    assert h.post(world.lea, f"/servers/{sid}/new-world", body).status_code == 403
    assert h.post(world.oscar, f"/servers/{sid}/new-world", {"seed": "a\nb"}).status_code == 422
    revision = h.status(world.oscar, sid)["config_revision"]
    r = h.post(world.oscar, f"/servers/{sid}/new-world", body)
    assert r.status_code == 200, r.text
    status = r.json()
    assert status["properties"]["level-seed"] == "Les copains"
    assert status["properties"]["level-type"] == "minecraft:large_biomes"
    assert status["properties"]["generate-structures"] is False
    assert status["config_revision"] == revision + 1
    versions = h.get(world.oscar, f"/servers/{sid}/versions").json()
    new, previous = versions[0], versions[1]
    assert new["parent_version_id"] == old["version_id"] and new["label"] == "Nouveau monde (graine : Les copains)"
    assert previous["version_id"] == old["version_id"] and previous["pinned"]
    paths = [f["path"] for f in h.get(world.oscar, f"/servers/{sid}/versions/{new['version_id']}/manifest").json()["files"]]
    assert paths == ["config/mod.toml", "ops.json", "server.properties"]

    # The old world can come back.
    r = h.post(world.oscar, f"/servers/{sid}/restore", {"version_id": old["version_id"]})
    assert r.status_code == 201 and r.json()["manifest_sha256"] == old["manifest_sha256"]


def test_new_world_before_the_first_start_only_sets_the_seed(world) -> None:
    h, sid = world.h, world.server_id
    r = h.post(world.oscar, f"/servers/{sid}/new-world", {})
    assert r.status_code == 200, r.text
    assert r.json()["properties"]["level-seed"] == ""
    assert h.get(world.oscar, f"/servers/{sid}/versions").json() == []


def test_only_the_owner_deletes_a_stopped_server_and_its_whole_history(world) -> None:
    h, sid = world.h, world.server_id
    pc_o, _, _ = start_on_oscar(world)
    upload(world, pc_o, WORLD)
    v1 = commit(world, pc_o, WORLD, None).json()
    v2 = commit(world, pc_o, {**WORLD, "world/level.dat": b"level-2"}, v1["version_id"])
    assert v2.status_code == 422  # level-2 not uploaded: still only v1
    h.post(world.oscar, f"/servers/{sid}/versions/{v1['version_id']}/pin", {"pinned": True})
    name = h.status(world.oscar, sid)["name"]
    r = h.post(world.oscar, f"/servers/{sid}/delete", {"name": name})
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_running"

    h.post(world.oscar, f"/servers/{sid}/stop", {"countdown_s": 0})
    pc_o.release(sid)
    assert h.post(world.lea, f"/servers/{sid}/delete", {"name": name}).status_code == 403
    r = h.post(world.oscar, f"/servers/{sid}/delete", {"name": name + "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "name_mismatch"
    r = h.post(world.oscar, f"/servers/{sid}/delete", {"name": name})
    assert r.status_code == 200, r.text
    assert h.get(world.oscar, f"/servers/{sid}").status_code == 404
    assert h.get(world.oscar, f"/groups/{world.group_id}/servers").json() == []
    conn = h.ctx.conn
    for table in ("world_versions", "leases", "player_access", "server_host_prefs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE server_id = ?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM version_blobs WHERE version_id = ?", (v1["version_id"],)).fetchone()[0] == 0
    row = conn.execute("SELECT payload_json FROM events WHERE type = 'server_deleted'").fetchone()
    assert name in row[0]

    # Its blocks are freed by the cleanup, once the grace period is over.
    from mdh_server.storage.retention import RetentionPolicy, collect
    report = collect(conn, h.ctx.store, h.ctx.now() + 10**6, RetentionPolicy())
    assert report.blobs_deleted == len(manifest_of(WORLD).block_hashes())
