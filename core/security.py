import hashlib
import time
from functools import wraps

from django.conf import settings
from django.db.models import F
from ipaddress import ip_address
from django.shortcuts import render

from .models import AuthRateBucket


def client_ip(request):
    hops = settings.TRUSTED_PROXY_HOPS
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
    if hops and len(forwarded) >= hops:
        try:
            return str(ip_address(forwarded[-hops].strip()))
        except ValueError:
            pass
    return request.META.get("REMOTE_ADDR", "unknown")


# Shared across workers; proxy trust is off locally and explicit on Cloud Run.
def rate_limit(scope, limit=10, seconds=900):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == "POST":
                ip = client_ip(request)
                key = hashlib.sha256(f"{scope}:{ip}".encode()).hexdigest()
                window = int(time.time()) // seconds
                bucket, _ = AuthRateBucket.objects.get_or_create(
                    key=key, defaults={"window": window}
                )
                # Each update is an atomic autocommit statement, including on
                # SQLite. No row-lock-dependent read/modify/write transaction.
                AuthRateBucket.objects.filter(pk=bucket.pk, window__lt=window).update(
                    window=window, attempts=0
                )
                AuthRateBucket.objects.filter(pk=bucket.pk).update(
                    attempts=F("attempts") + 1
                )
                bucket.refresh_from_db(fields=["attempts"])
                if bucket.attempts > limit:
                    response = render(
                        request,
                        "core/error.html",
                        {
                            "title": "Too many attempts",
                            "explanation": "Please wait before trying again.",
                        },
                        status=429,
                    )
                    response["Retry-After"] = str(seconds - int(time.time()) % seconds)
                    return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorate
