import hashlib
import logging
import secrets
from datetime import timedelta
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone
from billing.models import Customer
from core.security import count_attempt
from .models import PortalToken

logger = logging.getLogger(__name__)
LINK_LIFETIME = timedelta(minutes=30)
SESSION_KEY = "portal_customers"
MAX_RECORDS_PER_REQUEST = 10
# Per recipient, whichever addresses the requests come from.
LINKS_PER_RECIPIENT_PER_HOUR = 3


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def send_link(customer):
    """Email a single-use sign-in link for this customer record."""
    if not customer.email:
        return False
    raw = secrets.token_urlsafe(32)
    PortalToken.objects.create(
        customer=customer, digest=digest(raw), expires_at=timezone.now() + LINK_LIFETIME
    )
    link = settings.MANDALI_BASE_URL + reverse("portal_enter", args=[raw])
    business = customer.workspace.name
    try:
        send_mail(
            f"Your invoices from {business}",
            f"Open your invoices, receipts, and subscriptions with {business}:\n{link}\n\n"
            "The link works once and expires in 30 minutes. If you did not ask for it, ignore this email.",
            settings.DEFAULT_FROM_EMAIL,
            [customer.email],
        )
    except Exception:
        logger.error("Customer portal email delivery failed.")
        return False
    return True


def request_links(email):
    """Send a link for each business that has this email on a customer record.

    The caller always shows the same response, so the form never reveals
    whether an email is known.
    """
    address = email.strip().lower()
    if count_attempt("portal-recipient", address, 3600) > LINKS_PER_RECIPIENT_PER_HOUR:
        return
    customers = Customer.objects.filter(email__iexact=address).exclude(email="").select_related("workspace")
    for customer in customers[:MAX_RECORDS_PER_REQUEST]:
        send_link(customer)


def claim(raw):
    """Use a sign-in link once. Returns the customer, or None."""
    token = PortalToken.objects.filter(digest=digest(raw)).select_related("customer").first()
    if token is None:
        return None
    # A conditional update makes the link single use even under double submits.
    if not PortalToken.objects.filter(
        pk=token.pk, used_at=None, expires_at__gt=timezone.now()
    ).update(used_at=timezone.now()):
        return None
    return token.customer


def signed_in(request):
    return list(request.session.get(SESSION_KEY, []))


def add_customer(request, customer):
    ids = signed_in(request)
    # A fresh session key on sign-in prevents session fixation.
    request.session.cycle_key()
    if customer.pk not in ids:
        ids.append(customer.pk)
    request.session[SESSION_KEY] = ids


def customers_for(request):
    return Customer.objects.filter(pk__in=signed_in(request)).select_related("workspace")
