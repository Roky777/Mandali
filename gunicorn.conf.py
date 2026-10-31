"""Cloud Run sends SIGTERM before shutdown; Gunicorn drains in-flight work."""

import os

bind = f"0.0.0.0:{int(os.getenv('PORT', '8080'))}"
workers = 2
threads = 4
worker_class = "gthread"
timeout = 60
graceful_timeout = 8
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
# Cloud Run provides request logs. Avoid duplicating token-bearing request URLs.
accesslog = None
errorlog = "-"
capture_output = True
