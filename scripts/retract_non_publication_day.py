"""Retract an operator-declared non-publication day (audited).

Use when a declaration was a mistake. Days from the official BoI calendar can't be
retracted (they change only by migration).

    python scripts/retract_non_publication_day.py --date 2026-12-02 \
        --reason "Declared by mistake: BoI did publish" --actor alice

Exit codes: 0 retracted, 2 invalid input, 3 not an operator-declared day, 1 storage error.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _operator_cli import run  # noqa: E402

from rates.clock import SystemClock  # noqa: E402
from rates.services.operator import retract_non_publication_day  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True, help="Who is retracting (recorded in audit)")
    parser.add_argument("--database-url", default=None, help="Overrides DATABASE_URL")
    args = parser.parse_args(argv)
    return run(
        args.database_url,
        lambda engine: retract_non_publication_day(engine, args.date, args.reason, args.actor, SystemClock()),
        lambda _event: f"retracted {args.date.isoformat()}",
    )


if __name__ == "__main__":
    sys.exit(main())
