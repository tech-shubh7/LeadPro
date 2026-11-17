"""Entry point for the console: `python -m app.server` (or `leadpro web`).

Refuses unsafe binds: anything beyond localhost needs an access key (the console shows personal data)
and TLS (the key and that data must not cross a network in clear text).
"""

from pathlib import Path

import uvicorn

from app.config.settings import get_settings
from app.utils.logger import configure_logging, get_logger

logger = get_logger(__name__)
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UnsafeBindError(ValueError):
    """The requested bind would expose the console without an access key or without TLS."""


def check_bind(host: str, auth_token: str, certfile: Path | None, keyfile: Path | None) -> None:
    public = host not in LOCAL_HOSTS
    if public and not auth_token:
        raise UnsafeBindError(
            "Refusing to bind beyond localhost without WEB_AUTH_TOKEN set: the console shows personal data."
        )
    if public and (certfile is None or keyfile is None):
        raise UnsafeBindError("Refusing a public HTTP bind: supply a TLS certificate and key.")
    if (certfile is None) != (keyfile is None):
        raise UnsafeBindError("TLS needs both a certificate and a key.")


def serve(
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
    certfile: Path | None = None,
    keyfile: Path | None = None,
) -> None:
    settings = get_settings()
    host, port = host or settings.web_host, port or settings.web_port
    configure_logging(settings.log_level)
    check_bind(host, settings.web_auth_token, certfile, keyfile)
    logger.info("console on %s://%s:%s", "https" if certfile else "http", host, port)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        ssl_certfile=str(certfile) if certfile else None,
        ssl_keyfile=str(keyfile) if keyfile else None,
    )


if __name__ == "__main__":
    serve(reload=get_settings().is_dev)
