"""Validated, side-effect-free deployment configuration."""

from pathlib import Path
from urllib.parse import urlsplit
import re

from django.core.exceptions import ImproperlyConfigured


def canonical_url(env, debug):
    raw = (
        env.get("MANDALI_BASE_URL", "http://127.0.0.1:8000" if debug else "")
        .strip()
        .rstrip("/")
    )
    url = urlsplit(raw)
    if (
        not url.hostname
        or url.scheme not in ({"http", "https"} if debug else {"https"})
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ImproperlyConfigured(
            "MANDALI_BASE_URL must be an HTTPS origin, for example https://mandali-xyz.run.app (no path)."
        )
    return raw


def allowed_hosts(env, debug, base_url):
    hosts = [
        part.strip()
        for part in env.get("DJANGO_ALLOWED_HOSTS", "").split(",")
        if part.strip()
    ]
    hosts = hosts or (
        ["localhost", "127.0.0.1", "[::1]"] if debug else [urlsplit(base_url).hostname]
    )
    if any(
        "/" in host or "://" in host or (host == "*" and not debug) for host in hosts
    ):
        raise ImproperlyConfigured(
            "DJANGO_ALLOWED_HOSTS must contain hostnames, without schemes, paths, or a production wildcard."
        )
    return hosts


def database_config(env, debug, base_dir: Path):
    connection = env.get("CLOUD_SQL_CONNECTION_NAME", "").strip()
    host = env.get("DB_HOST", "").strip()
    if connection:
        if not re.fullmatch(r"[a-z][a-z0-9-]*:[a-z0-9-]+:[a-z][a-z0-9-]*", connection):
            raise ImproperlyConfigured(
                "CLOUD_SQL_CONNECTION_NAME must be project:region:instance."
            )
        if host:
            raise ImproperlyConfigured(
                "Use CLOUD_SQL_CONNECTION_NAME or DB_HOST, not both."
            )
        host = "/cloudsql/" + connection
    if not host:
        if not debug:
            raise ImproperlyConfigured(
                "Production requires PostgreSQL: configure CLOUD_SQL_CONNECTION_NAME, DB_NAME, DB_USER, and DB_PASSWORD."
            )
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": base_dir / "db.sqlite3",
            "OPTIONS": {"timeout": 20},
        }
    missing = [
        name for name in ["DB_NAME", "DB_USER", "DB_PASSWORD"] if not env.get(name)
    ]
    if missing:
        raise ImproperlyConfigured("Missing PostgreSQL settings: " + ", ".join(missing))
    # The Cloud SQL Auth Proxy encrypts the remote hop; the local socket itself
    # has no TLS layer. Direct TCP connections require verified TLS in production.
    socket = host.startswith("/")
    sslmode = env.get("DB_SSLMODE", "disable" if socket or debug else "verify-full")
    if not debug and not socket and sslmode != "verify-full":
        raise ImproperlyConfigured(
            "Production TCP PostgreSQL requires DB_SSLMODE=verify-full and a trusted server certificate."
        )
    if not debug and socket and not connection:
        raise ImproperlyConfigured(
            "Production Unix sockets must use CLOUD_SQL_CONNECTION_NAME."
        )
    options = {"connect_timeout": 10, "sslmode": sslmode}
    if env.get("DB_SSLROOTCERT"):
        options["sslrootcert"] = env["DB_SSLROOTCERT"]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": host,
        "PORT": env.get("DB_PORT", "5432"),
        "NAME": env["DB_NAME"],
        "USER": env["DB_USER"],
        "PASSWORD": env["DB_PASSWORD"],
        "CONN_MAX_AGE": int(env.get("DB_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": options,
    }
