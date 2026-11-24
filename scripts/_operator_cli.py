"""Shared plumbing for the operator scripts: one engine, one action, uniform exit codes.

Exit codes: 0 done, 2 invalid input, 3 no-op or not allowed (nothing changed),
1 storage error.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.session import create_engine
from rates.domain.errors import AlreadyInState, InvalidRequest
from rates.domain.models import AuditEvent
from rates.settings import Settings


def run(
    database_url: str | None,
    action: Callable[[AsyncEngine], Awaitable[AuditEvent]],
    success: Callable[[AuditEvent], str],
) -> int:
    async def _run() -> int:
        engine = create_engine(database_url or Settings().database_url)
        try:
            event = await action(engine)
        except InvalidRequest as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except AlreadyInState as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        except (SQLAlchemyError, OSError) as exc:
            print(f"error: storage unavailable: {exc}", file=sys.stderr)
            return 1
        finally:
            await engine.dispose()
        print(f"{success(event)} (audit event {event.id})")
        return 0

    return asyncio.run(_run())
