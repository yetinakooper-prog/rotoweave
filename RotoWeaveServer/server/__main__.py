from __future__ import annotations

import threading

import uvicorn

from .api import create_admin_app, create_remote_app
from .config import RemoteServerSettings
from .service import RemoteService


def main(settings: RemoteServerSettings | None = None) -> None:
    settings = settings or RemoteServerSettings()
    service = RemoteService(settings)
    service.start()
    admin = create_admin_app(service)

    def run_admin() -> None:
        uvicorn.run(
            admin,
            host=settings.admin_host,
            port=settings.admin_port,
            log_level="info",
        )

    thread = threading.Thread(target=run_admin, name="remote-admin", daemon=True)
    thread.start()
    try:
        uvicorn.run(
            create_remote_app(settings, service=service, manage_lifecycle=False),
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
        )
    finally:
        service.stop()


if __name__ == "__main__":
    main()
