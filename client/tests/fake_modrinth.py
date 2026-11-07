"""A small fake of Modrinth (API and CDN) for the tests: same JSON shapes as the real one
(checked against api.modrinth.com on 2026-11-01)."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx


class FakeModrinth:
    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}
        self.versions: dict[str, list[dict]] = {}
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []

    def add(self, project_id: str, slug: str, *, loaders=("fabric",), game_versions=("26.3",),
            version_id: str | None = None, deps=(), incompatible=(), server_side="required",
            content: bytes | None = None, version_type="release", version_number: str | None = None,
            project_type="mod", client_side="optional", title: str | None = None) -> dict:
        version_id = version_id or (project_id[:4] + "v" * 4)
        content = content if content is not None else f"jar of {slug} {version_id}".encode()
        filename = f"{slug}-1.0+{game_versions[0]}." + {"resourcepack": "zip", "modpack": "mrpack", "datapack": "zip"}.get(project_type,
                                                                                                    "jar")
        url = f"https://cdn.modrinth.com/data/{project_id}/versions/{version_id}/{filename.replace('+', '%2B')}"
        self.files[url] = content
        self.projects.setdefault(project_id, {"id": project_id, "slug": slug, "title": title or slug.capitalize(),
                                              "server_side": server_side, "client_side": client_side,
                                              "project_type": project_type})
        version = {"id": version_id, "project_id": project_id, "version_number": version_number or f"1.0-{loaders[0]}",
                   "game_versions": list(game_versions), "loaders": list(loaders), "version_type": version_type,
                   "dependencies": [{"version_id": None, "project_id": d, "file_name": None,
                                     "dependency_type": "required"} for d in deps]
                   + [{"version_id": None, "project_id": d, "file_name": None,
                       "dependency_type": "incompatible"} for d in incompatible],
                   "files": [{"hashes": {"sha512": hashlib.sha512(content).hexdigest(),
                                         "sha1": hashlib.sha1(content).hexdigest()},
                              "url": url, "filename": filename, "primary": True, "size": len(content)}]}
        self.versions.setdefault(project_id, []).insert(0, version)  # newest first, like Modrinth
        return version

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if request.url.host == "cdn.modrinth.com":
            body = self.files.get(url)
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
        path = urlparse(url).path.removeprefix("/v2")
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        parts = path.strip("/").split("/")
        if parts == ["search"]:
            facets = json.loads(query["facets"])
            if facets[0] == ["project_type:modpack"]:
                hits = [{"project_id": pid, "slug": p["slug"], "title": p["title"], "description": "a modpack",
                         "downloads": 5000}
                        for pid, p in self.projects.items()
                        if query.get("query", "") in p["slug"] and p["project_type"] == "modpack"]
                return httpx.Response(200, json={"hits": hits, "total_hits": len(hits)})
            if facets[0] == ["project_type:datapack"]:
                version = facets[1][0].split(":")[1]
                hits = [{"project_id": pid, "slug": p["slug"], "title": p["title"], "description": "a datapack",
                         "downloads": 800}
                        for pid, p in self.projects.items()
                        if query.get("query", "") in p["slug"] and p["project_type"] == "datapack"
                        and any(version in v["game_versions"] for v in self.versions[pid])]
                return httpx.Response(200, json={"hits": hits, "total_hits": len(hits)})
            packs = facets[0] == ["project_type:resourcepack"]
            loaders = {"minecraft"} if packs else {f.split(":")[1] for f in facets[0]}
            version = facets[1][0].split(":")[1]
            hits = [{"project_id": pid, "slug": p["slug"], "title": p["title"], "description": f"{p['slug']} mod",
                     "downloads": 1000}
                    for pid, p in self.projects.items()
                    if query.get("query", "") in p["slug"] and p["server_side"] != "unsupported"
                    and (p["project_type"] == "resourcepack") == packs and p["project_type"] not in ("modpack", "datapack")
                    and any(loaders & set(v["loaders"]) and version in v["game_versions"]
                            for v in self.versions[pid])]
            return httpx.Response(200, json={"hits": hits, "total_hits": len(hits)})
        if len(parts) == 2 and parts[0] == "project":
            project = self.projects.get(parts[1]) or next(
                (p for p in self.projects.values() if p["slug"] == parts[1]), None)
            return httpx.Response(200, json=project) if project else httpx.Response(404)
        if len(parts) == 3 and parts[0] == "project" and parts[2] == "version":
            loaders = set(json.loads(query["loaders"]))
            games = set(json.loads(query["game_versions"])) if "game_versions" in query else None
            found = [v for v in self.versions.get(parts[1], [])
                     if loaders & set(v["loaders"]) and (games is None or games & set(v["game_versions"]))]
            return httpx.Response(200, json=found)
        if parts == ["projects"]:
            ids = json.loads(query["ids"])
            return httpx.Response(200, json=[self.projects[i] for i in ids if i in self.projects])
        if parts == ["versions"]:
            ids = set(json.loads(query["ids"]))
            return httpx.Response(200, json=[v for vs in self.versions.values() for v in vs if v["id"] in ids])
        return httpx.Response(404)
