from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from rates.api.problems import problem_openapi
from rates.api.schemas import ExchangeRateOut, ReadinessOut
from rates.services.rate_query import RateQueryService

CURRENCY_PATTERN = r"^[A-Za-z]{3}$"

router = APIRouter()


def get_service(request: Request) -> RateQueryService:
    return request.app.state.rate_query


CurrencyCode = Annotated[
    str,
    Query(pattern=CURRENCY_PATTERN, description="ISO 4217 alphabetic code (case-insensitive)", examples=["USD"]),
]


@router.get(
    "/api/v1/exchange-rates",
    response_model=ExchangeRateOut,
    summary="Latest BoI representative rate for a currency pair",
    description=(
        "Returns the latest ingested Bank of Israel representative rate with its publication "
        "date and an eligibility assessment. **Consumers must check `eligibility.status` == "
        "`ELIGIBLE`** before using the rate: an out-of-date rate is returned with status 200 "
        "and `NOT_ELIGIBLE`. Rates are decimal strings. 1 `base` = `rate` `quote`."
    ),
    responses={
        400: problem_openapi("INVALID_REQUEST"),
        404: problem_openapi("RATE_NOT_FOUND"),
        422: problem_openapi("UNSUPPORTED_CURRENCY_PAIR"),
        503: problem_openapi("POLICY_NOT_EVALUABLE", "STORAGE_UNAVAILABLE"),
    },
)
async def get_exchange_rate(
    base: CurrencyCode,
    quote: CurrencyCode,
    service: Annotated[RateQueryService, Depends(get_service)],
) -> ExchangeRateOut:
    view = await service.get_rate(base.upper(), quote.upper())
    return ExchangeRateOut.from_view(view)


@router.get("/health/live", summary="Process is up", tags=["health"])
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/ready",
    response_model=ReadinessOut,
    summary="Database reachable; reports the latest ingestion time",
    tags=["health"],
    responses={503: problem_openapi("STORAGE_UNAVAILABLE")},
)
async def ready(service: Annotated[RateQueryService, Depends(get_service)]) -> ReadinessOut:
    readiness = await service.readiness()
    return ReadinessOut(latest_retrieved_at=readiness.latest_retrieved_at)
