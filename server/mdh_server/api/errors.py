"""Uniform JSON errors: ``{"error": {"code": ..., "message": ..., "details"?: {...}}}``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mdh_server.coordination.lease import LeaseError


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
        #: Machine-readable context for the client (e.g. why a PC is not eligible).
        self.details = details


def not_found(what: str = "resource") -> ApiError:
    # Used for "does not exist" *and* "exists but you may not see it": no existence oracle.
    return ApiError(404, "not_found", f"{what} not found")


def forbidden(message: str = "insufficient permissions") -> ApiError:
    return ApiError(403, "forbidden", message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
        return JSONResponse(
            {"error": error},
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(LeaseError)
    async def _lease_error(_: Request, exc: LeaseError) -> JSONResponse:
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=409)
