from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from rates.api.problems import problem_openapi
from rates.api.schemas import ExchangeRateOut, NotEligiblePairOut, ReadinessOut
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
        "and `NOT_ELIGIBLE`. Rates are decimal strings. 1 `base` = `rate` `quote`.\n\n"
        "**Direction.** Only `base`=USD/EUR/GBP with `quote`=ILS is served; `ILS/USD` returns 422 "
        "by design. To convert a shekel amount into dollars, divide by the USD/ILS rate: "
        "10,000 ILS / 3.343 = 2,991.3251... USD (2,991.33 half-up, 2,991.32 rounded down). "
        "Rounding is the consumer's money rule; this API never rounds."
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
    summary="Database reachable; reports the latest retrieval and last successful ingestion",
    tags=["health"],
    responses={503: problem_openapi("STORAGE_UNAVAILABLE")},
)
async def ready(service: Annotated[RateQueryService, Depends(get_service)]) -> ReadinessOut:
    readiness = await service.readiness()
    return ReadinessOut(
        latest_retrieved_at=readiness.latest_retrieved_at,
        last_successful_ingestion_at=readiness.last_successful_ingestion_at,
        not_eligible_pairs=[
            NotEligiblePairOut(pair=p.pair, reason=p.reason, publication_date=p.publication_date)
            for p in readiness.not_eligible_pairs
        ],
        calendar_coverage_ends_on=readiness.calendar_coverage_ends_on,
        calendar_expiry_warning=readiness.calendar_expiry_warning,
    )
