"""Controlled pilot: limit who can create accounts and businesses.

MANDALI_PILOT_ALLOWLIST holds comma-separated email addresses and @domains,
for example "owner@shop.in,@trustedstudio.in". When it is empty, signup is
open. Staff invited by a pilot business can always join through their
invitation, so the allowlist only gates new businesses.
"""

from django.conf import settings


def allowlist():
    return [entry.strip().lower() for entry in settings.MANDALI_PILOT_ALLOWLIST.split(",") if entry.strip()]


def allowed(email):
    entries = allowlist()
    if not entries:
        return True
    email = (email or "").strip().lower()
    domain = "@" + email.rpartition("@")[2]
    return email in entries or domain in entries


MESSAGE = "Mandali is in a private pilot. Ask the Mandali team to add your email to the pilot."
