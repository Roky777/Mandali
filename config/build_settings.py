"""Collect static assets without runtime credentials or a database connection.

Only the Docker build uses this module. The running service uses config.settings.
"""

import os

os.environ["DJANGO_DEBUG"] = "1"
os.environ["MANDALI_BASE_URL"] = "http://127.0.0.1:8000"
for name in [
    "CLOUD_SQL_CONNECTION_NAME",
    "DB_HOST",
    "DJANGO_TRUST_CLOUD_RUN_PROXY",
    "DJANGO_TRUSTED_PROXY_HOPS",
]:
    os.environ.pop(name, None)
from .settings import *  # noqa: F403, E402

STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
}  # noqa: F405
