"""Declare an unexpected BoI non-publication day (audited).

Use when BoI did not publish on a business day that is not in the official calendar
(e.g. "lack of data"). Until declared, reads report NOT_ELIGIBLE/PUBLICATION_OVERDUE.
The date must be between today and the policy's max age before today (Israel time);
--force overrides that and is recorded in the audit. Undo with
retract_non_publication_day.py.

    python scripts/declare_non_publication_day.py --date 2026-12-02 \
        --reason "BoI did not publish: no FX trading data" --actor alice

Exit codes: 0 declared, 2 invalid input or outside the window, 3 already declared,
1 storage error.
"""

import argparse
import asyncio
import sys
from datetime import date

from sqlalchemy.exc import SQLAlchemyError

from rates.adapters.db.session import create_engine
from rates.clock import SystemClock
from rates.domain.errors import AlreadyInState, InvalidRequest
from rates.services.operator import declare_non_publication_day
from rates.settings import Settings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True, help="Who is declaring (recorded in audit)")
    parser.add_argument("--force", action="store_true", help="Allow a date outside the window (audited)")
    parser.add_argument("--database-url", default=None, help="Overrides DATABASE_URL")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine(args.database_url or Settings().database_url)
    try:
        event = await declare_non_publication_day(
            engine, args.date, args.reason, args.actor, SystemClock(), force=args.force
        )
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
    print(f"declared {args.date.isoformat()} as a non-publication day (audit event {event.id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
