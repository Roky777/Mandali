"""Exercise the built production image with dummy configuration, no Cloud access."""

import json
import re
import sys
import urllib.error
import urllib.request

base = sys.argv[1]
headers = {"Host": "mandali.example.com", "X-Forwarded-Proto": "https"}


def fetch(path):
    try:
        return urllib.request.urlopen(
            urllib.request.Request(base + path, headers=headers), timeout=15
        )
    except urllib.error.HTTPError as response:
        return response


live = fetch("/health/live/")
assert live.status == 200 and json.load(live) == {"status": "ok"}
login = fetch("/login/")
assert login.status == 200, login.status
assert "no-store" in login.headers["Cache-Control"]
html = login.read().decode()
stylesheet = re.search(r'href="(/static/[^\"]+\.css)"', html).group(1)
assert re.search(r"\.[a-f0-9]{12}\.css$", stylesheet), stylesheet
asset = fetch(stylesheet)
assert asset.status == 200 and "text/css" in asset.headers["Content-Type"]
assert b"--color-accent" in asset.read()
ready = fetch("/health/ready/")
assert ready.status == 503, ready.status
assert json.load(ready) == {"status": "unavailable"}
print(
    "Production container: login, hashed CSS, liveness, and generic database-failure response passed."
)
