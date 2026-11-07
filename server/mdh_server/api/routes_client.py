"""Application updates: the published installers (docs/API.md section 8 ter).

``?platform=`` picks the system (0.9.0); without it, Windows, as for the applications before.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from mdh_server.api.errors import not_found
from mdh_server.auth.deps import Ctx, CurrentUser
from mdh_shared.models.api import ClientPlatform, ClientRelease

router = APIRouter(tags=["client"])

Platform = Query("windows")


# Fields left empty are left out: applications before 0.9.0 refuse any field they do not know
# ("platform").
@router.get("/client/release", response_model_exclude_none=True)
async def client_release(_: CurrentUser, ctx: Ctx, platform: ClientPlatform = Platform) -> ClientRelease:
    found = await run_in_threadpool(ctx.releases[platform].current)
    if found is None:
        raise not_found("client release")
    return found[0]


@router.get("/client/release/installer")
async def client_installer(_: CurrentUser, ctx: Ctx, platform: ClientPlatform = Platform) -> FileResponse:
    found = await run_in_threadpool(ctx.releases[platform].current)
    if found is None:
        raise not_found("client release")
    path = found[1]
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"Cache-Control": "no-store"})
