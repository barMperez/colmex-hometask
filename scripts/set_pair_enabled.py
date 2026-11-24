"""Enable or disable a configured currency pair (audited).

Use e.g. when BoI stops sending a currency: disabling the pair stops it being served,
ingested and reported, so refresh attempts become successful again. New pairs are added
by migration, not here.

    python scripts/set_pair_enabled.py --pair GBP/ILS --disable \
        --reason "BoI stopped publishing GBP" --actor alice

Exit codes: 0 changed, 2 invalid input or unknown pair, 3 already in that state,
1 storage error.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _operator_cli import run  # noqa: E402

from rates.clock import SystemClock  # noqa: E402
from rates.services.operator import set_pair_enabled  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pair", required=True, help="BASE/QUOTE, e.g. GBP/ILS")
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--actor", required=True, help="Who is changing it (recorded in audit)")
    parser.add_argument("--database-url", default=None, help="Overrides DATABASE_URL")
    args = parser.parse_args(argv)
    enabled = bool(args.enable)
    return run(
        args.database_url,
        lambda engine: set_pair_enabled(engine, args.pair, enabled, args.reason, args.actor, SystemClock()),
        lambda _event: f"{args.pair.upper()} {'enabled' if enabled else 'disabled'}",
    )


if __name__ == "__main__":
    sys.exit(main())
