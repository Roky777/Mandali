from functools import wraps
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from billing import services as billing
from billing.models import CreditNote, Invoice, Payment, Source, Subscription
from billing.services import BillingError
from core.security import rate_limit
from core.views import workspace_access
from checkout.crypto import CheckoutError
from checkout.models import ShareLink
from checkout.services import available, link_url
from . import services
from .forms import LinkRequestForm

PORTAL_ACTOR = "Customer (portal)"
# Customers act only on subscriptions they hold, within the plan's policies.
CUSTOMER_ACTIONS = {"pause": "Pause", "resume": "Resume", "close": "Close", "renew": "Renew"}
VISIBLE_SUBSCRIPTIONS = [
    Subscription.Status.QUOTATION,
    Subscription.Status.CONFIRMED,
    Subscription.Status.ACTIVE,
    Subscription.Status.PAUSED,
    Subscription.Status.CLOSED,
]


def portal_login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not services.signed_in(request):
            return redirect("portal_request")
        return view(request, *args, **kwargs)

    return wrapped


def visible_invoices(request):
    # Drafts are internal. Hosted-checkout invoices are started by whoever
    # holds a public link and claims an email, so an unverified buyer could
    # place one under someone else's record: only real, paid ones are shown.
    return (
        Invoice.objects.filter(customer_id__in=services.signed_in(request))
        .exclude(status=Invoice.Status.DRAFT)
        .exclude(source_channel=Source.HOSTED, is_sandbox=True)
        .exclude(source_channel=Source.HOSTED, amount_paid=0)
    )


def own_invoice(request, pk):
    return get_object_or_404(visible_invoices(request).select_related("workspace"), pk=pk)


@rate_limit("portal-link", limit=5, seconds=3600)
def request_link(request):
    form = LinkRequestForm(request.POST or None)
    sent = False
    if request.method == "POST" and form.is_valid():
        services.request_links(form.cleaned_data["email"])
        sent = True
    return render(request, "portal/request.html", {"form": form, "sent": sent})


@rate_limit("portal-enter", limit=10)
def enter(request, token):
    # Opening the link only shows a button, so an email scanner that follows
    # links cannot use up the single-use token.
    if request.method == "POST":
        customer = services.claim(token)
        if customer is None:
            return render(request, "portal/expired.html", status=410)
        services.add_customer(request, customer)
        return redirect("portal_home")
    return render(request, "portal/enter.html")


@portal_login_required
def home(request):
    groups = []
    for customer in services.customers_for(request):
        invoices = list(visible_invoices(request).filter(customer=customer))
        groups.append(
            {
                "customer": customer,
                "open": [i for i in invoices if i.status == Invoice.Status.ISSUED],
                "settled": [i for i in invoices if i.status != Invoice.Status.ISSUED],
                "subscriptions": customer.subscription_set.filter(status__in=VISIBLE_SUBSCRIPTIONS).select_related("plan"),
            }
        )
    return render(request, "portal/home.html", {"groups": groups, "today": timezone.localdate()})


def pay_link(invoice):
    """An active payment link the business already shared for this invoice."""
    if invoice.status != Invoice.Status.ISSUED:
        return None
    for link in ShareLink.objects.filter(invoice=invoice).order_by("-created_at")[:5]:
        if available(link):
            try:
                return link_url(link)
            except CheckoutError:
                return None
    return None


@portal_login_required
def invoice(request, pk):
    found = own_invoice(request, pk)
    return render(
        request,
        "portal/invoice.html",
        {
            "invoice": found,
            "lines": found.lines.all(),
            "payments": found.payments.filter(reversed_at=None),
            "credit_notes": found.credit_notes.all(),
            "refunds": found.refunds.all(),
            "pay_url": pay_link(found),
        },
    )


@portal_login_required
def receipt(request, pk, payment_id):
    found = own_invoice(request, pk)
    payment = get_object_or_404(Payment, pk=payment_id, invoice=found, reversed_at=None)
    return render(request, "portal/document.html", {"invoice": found, "payment": payment, "sheet": "billing/receipt_sheet.html"})


@portal_login_required
def credit_note(request, pk):
    note = get_object_or_404(
        CreditNote.objects.select_related("invoice"), pk=pk, invoice__customer_id__in=services.signed_in(request)
    )
    return render(request, "portal/document.html", {"invoice": note.invoice, "note": note, "sheet": "billing/credit_note_sheet.html"})


@require_POST
@portal_login_required
def subscription_action(request, pk, action):
    if action not in CUSTOMER_ACTIONS:
        raise Http404()
    subscription = get_object_or_404(
        Subscription.objects.select_related("plan", "workspace"),
        pk=pk,
        customer_id__in=services.signed_in(request),
    )
    try:
        if action == "renew":
            from recurring.calendar import advance
            from recurring.services import renew
            if not subscription.plan_id:
                raise BillingError("A renewable plan is required.")
            anchor = max(subscription.expiry_date or timezone.localdate(), timezone.localdate())
            renew(subscription, None, advance(anchor, subscription.plan.period, anchor))
            billing.log(subscription.workspace, None, "Customer renewed subscription", subscription.number, actor_label=PORTAL_ACTOR)
        else:
            billing.transition(subscription, action, None, actor_label=PORTAL_ACTOR)
        messages.success(request, f"Subscription {subscription.number} updated.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect("portal_home")


@require_POST
def sign_out(request):
    request.session.pop(services.SESSION_KEY, None)
    return redirect("portal_request")


@require_POST
@workspace_access()
@rate_limit("portal-invite", limit=20, seconds=3600)
def invite(request, membership, customer_id):
    customer = get_object_or_404(membership.workspace.customer_set, pk=customer_id)
    if services.send_link(customer):
        billing.log(membership.workspace, request.user, "Portal link sent", customer.name)
        messages.success(request, f"A sign-in link was emailed to {customer.email}. It expires in 30 minutes.")
    else:
        messages.error(request, "No link was sent. Add an email address, or check the email settings.")
    return redirect("customer_detail", workspace_id=membership.workspace.pk, pk=customer.pk)
