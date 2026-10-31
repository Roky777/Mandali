import json
import logging
import time

logger = logging.getLogger("mandali.requests")
SLOW_MS = 2000


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the severity field Cloud Logging reads."""

    def format(self, record):
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class RequestTimingMiddleware:
    """Log each request's route pattern, status, and duration.

    The route pattern is logged instead of the path, so bearer tokens in URLs
    (portal links, payment links, invitations, password resets) never reach
    the logs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        match = getattr(request, "resolver_match", None)
        fields = {
            "route": match.route if match else "unmatched",
            "view": match.view_name if match else "",
            "method": request.method,
            "status": response.status_code,
            "duration_ms": elapsed,
        }
        level = logging.WARNING if elapsed >= SLOW_MS or response.status_code >= 500 else logging.INFO
        logger.log(level, "slow request" if elapsed >= SLOW_MS else "request", extra={"fields": fields})
        response["Server-Timing"] = f"app;dur={elapsed}"
        return response
