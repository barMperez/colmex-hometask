"""Entry point: ``python -m rates.api``. Workers scale freely; the API writes audit rows only, never rates."""

import uvicorn

from rates.settings import Settings, require_api_settings


def main() -> None:
    settings = Settings()
    try:
        require_api_settings(settings)  # exit before spawning workers that would crash-loop
    except RuntimeError as exc:
        raise SystemExit(f"rates.api: {exc}") from None
    uvicorn.run(
        "rates.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        workers=settings.api_workers,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
