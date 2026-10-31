import hashlib
import time
from functools import wraps

from django.db.models import F
from django.shortcuts import render

from .models import AuthRateBucket


# Shared across workers. A trusted proxy must set REMOTE_ADDR appropriately;
# client-supplied X-Forwarded-For headers are deliberately ignored.
def rate_limit(scope, limit=10, seconds=900):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == "POST":
                ip = request.META.get("REMOTE_ADDR", "unknown")
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
