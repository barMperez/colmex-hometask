from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.session import create_engine
from rates.api.auth import TokenAuthority
from rates.api.auth_routes import router as auth_router
from rates.api.problems import Problem, install_problem_handlers
from rates.api.routes import router
from rates.clock import Clock, SystemClock
from rates.logging_config import configure_logging
from rates.services.query_audit import QueryAuditService
from rates.services.rate_query import RateQueryService
from rates.settings import Settings, require_api_settings


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Application factory. Tests inject ``engine`` and ``clock``."""
    settings = settings or Settings()
    require_api_settings(settings)
    clk = clock or SystemClock()
    owns_engine = engine is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = engine or create_engine(settings.database_url)
        app.state.token_authority = TokenAuthority(settings, clk)
        app.state.query_audit = QueryAuditService(eng, clk)
        app.state.rate_query = RateQueryService(
            eng, clk, calendar_warning_days=settings.calendar_warning_days
        )
        try:
            yield
        finally:
            if owns_engine:
                await eng.dispose()

    if owns_engine:
        configure_logging(settings.log_level)

    app = FastAPI(
        title="Exchange Rates API",
        version="1.0.0",
        summary="Bank of Israel representative exchange rates with explicit eligibility",
        lifespan=lifespan,
    )
    install_problem_handlers(app)
    app.include_router(auth_router)
    app.include_router(router)

    original_openapi = app.openapi

    def openapi() -> dict:
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {})["Problem"] = (
            Problem.model_json_schema()
        )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
