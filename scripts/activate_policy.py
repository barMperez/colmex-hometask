"""Activate a new freshness-policy version (audited).

Creates version = highest + 1 with the given values (time zone copied from the current
version) and deactivates the current one, in one transaction. Takes effect on the next
evaluation; every eligibility verdict reports the version that produced it.

    python scripts/activate_policy.py --grace-minutes 30 --max-age-days 7 \
        --weekday-publish-time 15:15 --friday-publish-time 12:15 \
        --reason "Observed publications all before 15:45" --actor alice

Exit codes: 0 activated, 2 invalid input, 3 identical to the active version,
1 storage error.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _operator_cli import run  # noqa: E402

from rates.clock import SystemClock  # noqa: E402
from rates.domain.errors import InvalidRequest  # noqa: E402
from rates.services.operator import activate_policy, parse_hhmm  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grace-minutes", required=True, type=int)
    parser.add_argument("--max-age-days", required=True, type=int)
    parser.add_argument("--weekday-publish-time", required=True, help="HH:MM, Mon-Thu")
    parser.add_argument("--friday-publish-time", required=True, help="HH:MM")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True, help="Who is activating it (recorded in audit)")
    parser.add_argument("--database-url", default=None, help="Overrides DATABASE_URL")
    args = parser.parse_args(argv)
    try:
        weekday, friday = parse_hhmm(args.weekday_publish_time), parse_hhmm(args.friday_publish_time)
    except InvalidRequest as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run(
        args.database_url,
        lambda engine: activate_policy(
            engine,
            grace_minutes=args.grace_minutes,
            max_age_days=args.max_age_days,
            weekday_publish_time=weekday,
            friday_publish_time=friday,
            reason=args.reason,
            actor=args.actor,
            clock=SystemClock(),
        ),
        lambda event: f"activated policy version {event.entity_id}",
    )


if __name__ == "__main__":
    sys.exit(main())
