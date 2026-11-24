"""RFC 9457 problem-details responses. Every non-2xx response uses this shape."""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from rates.api.auth import AuthError
from rates.services.query_audit import QueryOutcome
from rates.domain.errors import (
    AuditUnavailable,
    InvalidRequest,
    PolicyNotEvaluable,
    RateNotFound,
    StorageUnavailable,
    UnsupportedPair,
)

log = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str


def problem_response(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = Problem(
        type=f"/problems/{code.lower().replace('_', '-')}",
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        code=code,
    )
    return JSONResponse(
        body.model_dump(), status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers
    )


def problem_openapi(*codes: str) -> dict:
    # The Problem schema itself is registered in app.create_app's OpenAPI hook.
    return {
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
        "description": "Problem details. code: " + ", ".join(codes),
    }


AUDIT_FLAG = "audit_rate_query"


async def audited_problem(
    request: Request, status: int, code: str, detail: str
) -> JSONResponse:
    """Problem response that is also written to the rate-query audit trail.

    Only for an authenticated request to an audited route (the route sets the flag,
    authentication sets the principal). The response carries no rate, so a failed
    audit write is logged and does not change it.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None and getattr(request.state, AUDIT_FLAG, False):
        try:
            await request.app.state.query_audit.record(
                principal.username, request.query_params, QueryOutcome(status, code=code)
            )
        except Exception as exc:  # noqa: BLE001 - never mask the original error
            log.error("audit.write_failed status=%d code=%s error=%r", status, code, exc)
    return problem_response(status, code, detail)


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError) -> JSONResponse:
        # Not audited: there is no user to attribute the request to.
        log.info("auth.rejected code=%s path=%s detail=%s", exc.code, request.url.path, exc)
        return problem_response(401, exc.code, str(exc), headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})
        detail = "Invalid or missing parameter(s): " + ", ".join(fields) + "."
        if {"base", "quote"} & set(fields):
            detail += " Expected 3-letter ISO 4217 codes."
        return await audited_problem(request, 400, "INVALID_REQUEST", detail)

    @app.exception_handler(InvalidRequest)
    async def _invalid(request: Request, exc: InvalidRequest) -> JSONResponse:
        return await audited_problem(request, 400, "INVALID_REQUEST", str(exc))

    @app.exception_handler(UnsupportedPair)
    async def _unsupported(request: Request, exc: UnsupportedPair) -> JSONResponse:
        return await audited_problem(request, 422, "UNSUPPORTED_CURRENCY_PAIR", str(exc))

    @app.exception_handler(RateNotFound)
    async def _not_found(request: Request, exc: RateNotFound) -> JSONResponse:
        return await audited_problem(request, 404, "RATE_NOT_FOUND", str(exc))

    @app.exception_handler(PolicyNotEvaluable)
    async def _policy(request: Request, exc: PolicyNotEvaluable) -> JSONResponse:
        log.error("policy_not_evaluable detail=%s", exc)
        return await audited_problem(request, 503, "POLICY_NOT_EVALUABLE", str(exc))

    @app.exception_handler(AuditUnavailable)
    async def _audit(_: Request, exc: AuditUnavailable) -> JSONResponse:
        # Not re-audited: the audit store is what just failed.
        log.error("audit_unavailable cause=%r", exc.__cause__)
        return problem_response(503, "AUDIT_UNAVAILABLE", str(exc))

    @app.exception_handler(StorageUnavailable)
    async def _storage(request: Request, exc: StorageUnavailable) -> JSONResponse:
        log.error("storage_unavailable cause=%r", exc.__cause__)
        return await audited_problem(request, 503, "STORAGE_UNAVAILABLE", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).name
        return await audited_problem(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return await audited_problem(request, 500, "INTERNAL_ERROR", "An unexpected error occurred")
