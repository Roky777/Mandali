import hashlib
import secrets
from datetime import timedelta
from django.db.models import Q
from django.utils import timezone
from .models import ApiKey


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create_key(workspace, user, name):
    raw = "mdl_" + secrets.token_urlsafe(32)
    key = ApiKey.objects.create(
        workspace=workspace,
        name=name,
        prefix=raw[:12],
        digest=digest(raw),
        created_by=user,
    )
    return key, raw


def authenticate(header):
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer" or not raw.startswith("mdl_"):
        return None
    key = (
        ApiKey.objects.select_related("workspace", "created_by")
        .filter(digest=digest(raw.strip()), revoked_at=None)
        .first()
    )
    if key:
        # Record use at most once a minute to avoid a write on every request.
        now = timezone.now()
        ApiKey.objects.filter(
            Q(last_used_at=None) | Q(last_used_at__lt=now - timedelta(minutes=1)),
            pk=key.pk,
        ).update(last_used_at=now)
    return key
