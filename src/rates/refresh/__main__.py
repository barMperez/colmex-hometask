"""Entry point: ``python -m rates.refresh``. Run exactly one instance."""

import asyncio
import contextlib
import logging
import signal

from rates.adapters.boi.client import BoiClient
from rates.adapters.db.session import create_engine
from rates.clock import SystemClock
from rates.logging_config import configure_logging
from rates.refresh.loop import RefreshLoop
from rates.services.ingestion import IngestionService
from rates.settings import Settings


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    clock = SystemClock()
    client = BoiClient(settings.boi_url, settings.boi_timeout_seconds, settings.boi_max_attempts)
    loop = RefreshLoop(engine, IngestionService(engine, client, clock), clock, settings)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not supported by the Windows event loop; Ctrl+C still works via KeyboardInterrupt.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await loop.run_forever(stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    logging.shutdown()
