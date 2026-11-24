"""RFC 9457 problem-details responses. Every non-2xx response uses this shape."""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from rates.domain.errors import (
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


def problem_response(status: int, code: str, detail: str) -> JSONResponse:
    body = Problem(
        type=f"/problems/{code.lower().replace('_', '-')}",
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        code=code,
    )
    return JSONResponse(body.model_dump(), status_code=status, media_type=PROBLEM_MEDIA_TYPE)


def problem_openapi(*codes: str) -> dict:
    # The Problem schema itself is registered in app.create_app's OpenAPI hook.
    return {
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
        "description": "Problem details. code: " + ", ".join(codes),
    }


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})
        detail = "Invalid or missing parameter(s): " + ", ".join(fields) + ". Expected 3-letter ISO 4217 codes."
        return problem_response(400, "INVALID_REQUEST", detail)

    @app.exception_handler(InvalidRequest)
    async def _invalid(_: Request, exc: InvalidRequest) -> JSONResponse:
        return problem_response(400, "INVALID_REQUEST", str(exc))

    @app.exception_handler(UnsupportedPair)
    async def _unsupported(_: Request, exc: UnsupportedPair) -> JSONResponse:
        return problem_response(422, "UNSUPPORTED_CURRENCY_PAIR", str(exc))

    @app.exception_handler(RateNotFound)
    async def _not_found(_: Request, exc: RateNotFound) -> JSONResponse:
        return problem_response(404, "RATE_NOT_FOUND", str(exc))

    @app.exception_handler(PolicyNotEvaluable)
    async def _policy(_: Request, exc: PolicyNotEvaluable) -> JSONResponse:
        log.error("policy_not_evaluable detail=%s", exc)
        return problem_response(503, "POLICY_NOT_EVALUABLE", str(exc))

    @app.exception_handler(StorageUnavailable)
    async def _storage(_: Request, exc: StorageUnavailable) -> JSONResponse:
        log.error("storage_unavailable cause=%r", exc.__cause__)
        return problem_response(503, "STORAGE_UNAVAILABLE", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).name
        return problem_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return problem_response(500, "INTERNAL_ERROR", "An unexpected error occurred")
