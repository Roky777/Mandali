from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.models import Membership
from core.views import context, workspace_access
from . import services
from .forms import (
    CreditNoteForm,
    RefundForm,
    CustomerForm,
    DiscountForm,
    InvoiceForm,
    LineForm,
    PaymentForm,
    PlanForm,
    ProductForm,
    ReasonForm,
    SubscriptionForm,
    TaxForm,
    TemplateForm,
    VariantForm,
)
from .models import (
    CreditNote,
    ZERO,
    Customer,
    Discount,
    Invoice,
    Payment,
    Plan,
    Product,
    ProductVariant,
    QuotationTemplate,
    Subscription,
    Tax,
    TemplateLine,
)
from .services import BillingError, log
from .templatetags.billing import inr

PAGE_SIZE = 25


def url(name, membership, *args):
    return reverse(name, args=[membership.workspace.pk, *args])


def form_page(request, membership, form, title, description, submit, cancel_url):
    return render(
        request,
        "core/form.html",
        {
            **context(membership),
            "form": form,
            "title": title,
            "description": description,
            "submit": submit,
            "cancel_url": cancel_url,
            "facts": [
                ("Currency", "Indian rupee (INR)"),
                ("Amounts", "Calculated by Mandali on save"),
            ],
        },
    )


def query_string(**params):
    params = {k: v for k, v in params.items() if v}
    return urlencode(params) + "&" if params else ""


def admin_needed(request):
    return render(
        request,
        "core/error.html",
        {
            "title": "Administrator access needed",
            "explanation": "Ask a business administrator to make this change.",
        },
        status=403,
    )


def save_scoped(request, membership, form, action):
    with transaction.atomic():
        obj = form.save()
        log(membership.workspace, request.user, action, str(obj))
    return obj


# Customers


@workspace_access()
def customers(request, membership):
    query = request.GET.get("q", "").strip()
    rows = Customer.objects.filter(workspace=membership.workspace).annotate(
        open_balance=Sum(
            "invoice__total",
            filter=Q(invoice__status=Invoice.Status.ISSUED),
            default=ZERO,
        )
        - Sum(
            "invoice__amount_paid",
            filter=Q(invoice__status=Invoice.Status.ISSUED),
            default=ZERO,
        )
    )
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(email__icontains=query))
    rows = rows.order_by("name", "id")
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "billing/customers.html",
        {
            **context(membership),
            "page": page,
            "query": query,
            "query_string": query_string(q=query),
        },
    )


@workspace_access()
def customer_edit(request, membership, pk=None):
    instance = pk and get_object_or_404(Customer, pk=pk, workspace=membership.workspace)
    form = CustomerForm(
        request.POST or None, instance=instance or None, workspace=membership.workspace
    )
    if request.method == "POST" and form.is_valid():
        customer = save_scoped(
            request, membership, form, "Customer updated" if pk else "Customer added"
        )
        messages.success(request, "Customer saved.")
        return redirect(url("customer_detail", membership, customer.pk))
    return form_page(
        request,
        membership,
        form,
        "Edit customer" if pk else "Add a customer",
        "The person or company you bill. Their details are copied onto each invoice when it is issued.",
        "Save customer",
        url("customer_detail", membership, pk) if pk else url("customers", membership),
    )


@workspace_access()
def customer_detail(request, membership, pk):
    customer = get_object_or_404(Customer, pk=pk, workspace=membership.workspace)
    return render(
        request,
        "billing/customer_detail.html",
        {
            **context(membership),
            "customer": customer,
            "invoices": customer.invoice_set.all()[:20],
            "subscriptions": customer.subscription_set.select_related("plan")[:20],
            "today": timezone.localdate(),
        },
    )


# Catalog: administrators configure it; staff can read it.

CATALOG = {
    "products": {
        "model": Product,
        "form": ProductForm,
        "title": "Products",
        "singular": "product",
        "description": "What you sell. Variants adjust a product's price.",
        "empty": "Add the services or goods you bill for. Products appear as lines on quotations and invoices.",
        "columns": ["Product", "Code", "Type", "Sales price", "Tax", "Status"],
        "cells": lambda o: [o.code or "None", o.get_kind_display(), inr(o.sales_price), o.tax or "No tax"],
        "related": ["tax"],
    },
    "plans": {
        "model": Plan,
        "form": PlanForm,
        "title": "Plans",
        "singular": "plan",
        "description": "Billing periods, prices, and the policies subscriptions follow.",
        "empty": "Add a daily, weekly, monthly, or yearly plan to start offering subscriptions.",
        "columns": ["Plan", "Code", "Period", "Price", "Minimum quantity", "Status"],
        "cells": lambda o: [o.code or "None", o.get_period_display(), inr(o.price), o.min_quantity],
        "related": [],
    },
    "taxes": {
        "model": Tax,
        "form": TaxForm,
        "title": "Taxes",
        "singular": "tax",
        "description": "Rates applied to product and plan lines.",
        "empty": "Add the tax rates you charge, such as GST 18%. Attach them to products and plans.",
        "columns": ["Tax", "Rate", "Status"],
        "cells": lambda o: [f"{o.rate.normalize():f}%"],
        "related": [],
    },
    "discounts": {
        "model": Discount,
        "form": DiscountForm,
        "title": "Discounts",
        "singular": "discount",
        "description": "Rules with eligibility, dates, and usage limits. Only administrators can create them.",
        "empty": "Add a fixed or percentage discount. Staff can apply it to eligible invoices.",
        "columns": ["Discount", "Valid", "Uses", "Status"],
        "cells": lambda o: [
            f"{o.starts_on:%d %b %Y} to {o.ends_on:%d %b %Y}"
            if o.starts_on and o.ends_on
            else (f"Until {o.ends_on:%d %b %Y}" if o.ends_on else "No end date"),
            f"{o.times_used} of {o.usage_limit}" if o.usage_limit is not None else f"{o.times_used}, no limit",
        ],
        "related": [],
    },
    "templates": {
        "model": QuotationTemplate,
        "form": TemplateForm,
        "title": "Quotation templates",
        "singular": "template",
        "description": "Reusable offers with a plan, lines, and a validity period.",
        "empty": "Save a standard offer once, then start new quotations from it.",
        "columns": ["Template", "Plan", "Valid for", "Status"],
        "cells": lambda o: [o.plan or "One-time", f"{o.validity_days} days"],
        "related": ["plan"],
    },
}


def catalog_spec(kind):
    if kind not in CATALOG:
        raise Http404("Unknown catalog section.")
    return CATALOG[kind]


@workspace_access()
def catalog(request, membership, kind="products"):
    spec = catalog_spec(kind)
    objects = spec["model"].objects.filter(workspace=membership.workspace).select_related(*spec["related"])
    detail = {"products": "product_detail", "templates": "template_detail"}.get(kind)
    is_admin = membership.role == Membership.Role.ADMIN

    def link(o):
        if detail:
            return url(detail, membership, o.pk)
        return url("catalog_edit", membership, kind, o.pk) if is_admin else None

    rows = [
        {
            "name": o.name,
            "url": link(o),
            "cells": list(zip(spec["columns"][1:], spec["cells"](o))),
            "active": o.is_active,
        }
        for o in objects
    ]
    return render(
        request,
        "billing/catalog.html",
        {
            **context(membership),
            "kind": kind,
            "spec": spec,
            "rows": rows,
            "sections": [(k, v["title"]) for k, v in CATALOG.items()],
        },
    )


@workspace_access(admin=True)
def catalog_edit(request, membership, kind, pk=None):
    spec = catalog_spec(kind)
    instance = pk and get_object_or_404(spec["model"], pk=pk, workspace=membership.workspace)
    form = spec["form"](
        request.POST or None, instance=instance or None, workspace=membership.workspace
    )
    if request.method == "POST" and form.is_valid():
        obj = save_scoped(
            request,
            membership,
            form,
            f"{spec['singular'].capitalize()} {'updated' if pk else 'added'}",
        )
        messages.success(request, f"{spec['singular'].capitalize()} saved.")
        if kind == "products":
            return redirect(url("product_detail", membership, obj.pk))
        if kind == "templates":
            return redirect(url("template_detail", membership, obj.pk))
        return redirect(url("catalog", membership, kind))
    return form_page(
        request,
        membership,
        form,
        f"Edit {spec['singular']}" if pk else f"Add a {spec['singular']}",
        spec["description"],
        f"Save {spec['singular']}",
        url("catalog", membership, kind),
    )


@workspace_access()
def product_detail(request, membership, pk):
    product = get_object_or_404(
        Product.objects.select_related("tax"), pk=pk, workspace=membership.workspace
    )
    form = VariantForm(request.POST or None, product=product)
    if request.method == "POST":
        if membership.role != Membership.Role.ADMIN:
            return admin_needed(request)
        if form.is_valid():
            save_scoped(request, membership, form, "Product variant added")
            messages.success(request, "Variant added.")
            return redirect(url("product_detail", membership, pk))
    return render(
        request,
        "billing/product_detail.html",
        {
            **context(membership),
            "product": product,
            "variants": product.variants.filter(is_active=True),
            "form": form,
        },
    )


@require_POST
@workspace_access(admin=True)
def variant_remove(request, membership, pk):
    variant = get_object_or_404(
        ProductVariant, pk=pk, product__workspace=membership.workspace, is_active=True
    )
    with transaction.atomic():
        # Existing lines keep their price snapshot, so variants are retired, not deleted.
        variant.is_active = False
        variant.save(update_fields=["is_active"])
        log(membership.workspace, request.user, "Product variant removed", variant.name)
    messages.success(request, "Variant removed from new lines.")
    return redirect(url("product_detail", membership, variant.product_id))


@workspace_access()
def template_detail(request, membership, pk):
    template = get_object_or_404(
        QuotationTemplate.objects.select_related("plan"),
        pk=pk,
        workspace=membership.workspace,
    )
    form = LineForm(request.POST or None, workspace=membership.workspace)
    if request.method == "POST":
        if membership.role != Membership.Role.ADMIN:
            return admin_needed(request)
        if form.is_valid():
            TemplateLine.objects.create(
                template=template,
                quantity=form.cleaned_data["quantity"],
                **form.cleaned_data["item"],
            )
            messages.success(request, "Line added to the template.")
            return redirect(url("template_detail", membership, pk))
    return render(
        request,
        "billing/template_detail.html",
        {
            **context(membership),
            "template": template,
            "lines": template.lines.select_related("plan", "product", "variant__product"),
            "form": form,
        },
    )


@require_POST
@workspace_access(admin=True)
def template_line_remove(request, membership, pk, line_id):
    template = get_object_or_404(QuotationTemplate, pk=pk, workspace=membership.workspace)
    template.lines.filter(pk=line_id).delete()
    messages.success(request, "Line removed from the template.")
    return redirect(url("template_detail", membership, pk))


# Quotations and subscriptions


@workspace_access()
def subscriptions(request, membership):
    status = request.GET.get("status", "")
    rows = Subscription.objects.filter(workspace=membership.workspace).select_related(
        "customer", "plan"
    )
    if status in Subscription.Status.values:
        rows = rows.filter(status=status)
    counts = dict(
        Subscription.objects.filter(workspace=membership.workspace)
        .values_list("status")
        .annotate(n=Count("id"))
    )
    return render(
        request,
        "billing/subscriptions.html",
        {
            **context(membership),
            "page": Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page")),
            "status": status,
            "query_string": query_string(status=status),
            "filters": [(v, l, counts.get(v, 0)) for v, l in Subscription.Status.choices],
        },
    )


@workspace_access()
def subscription_edit(request, membership, pk=None):
    instance = pk and get_object_or_404(
        Subscription, pk=pk, workspace=membership.workspace
    )
    if instance and instance.status != Subscription.Status.DRAFT:
        messages.error(request, "Only draft quotations can be edited.")
        return redirect(url("subscription_detail", membership, pk))
    form = SubscriptionForm(
        request.POST or None,
        instance=instance or None,
        workspace=membership.workspace,
        initial={"customer": request.GET.get("customer")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            if pk:
                with transaction.atomic():
                    services.claim_draft(Subscription, pk, Subscription.Status.DRAFT)
                    subscription = form.save()
                    log(membership.workspace, request.user, "Quotation updated", subscription.number)
            else:
                subscription = services.create_subscription(
                    form, request.user, form.cleaned_data.get("template")
                )
        except BillingError as error:
            messages.error(request, str(error))
            return redirect(url("subscriptions", membership))
        messages.success(request, "Quotation saved.")
        return redirect(url("subscription_detail", membership, subscription.pk))
    return form_page(
        request,
        membership,
        form,
        "Edit quotation" if pk else "New quotation",
        "Prepare an offer. Once the customer confirms, it becomes a subscription you can activate and invoice.",
        "Save quotation",
        url("subscription_detail", membership, pk) if pk else url("subscriptions", membership),
    )


ACTIONS = {
    "send": "Mark quotation as sent",
    "confirm": "Customer confirmed",
    "activate": "Activate subscription",
    "pause": "Pause",
    "resume": "Resume",
    "close": "Close subscription",
}


def lifecycle(status):
    S = Subscription.Status
    steps = [S.DRAFT, S.QUOTATION, S.CONFIRMED, S.ACTIVE, S.CLOSED]
    current = steps.index(S.ACTIVE if status == S.PAUSED else S.QUOTATION if status == S.EXPIRED else status)
    return [
        {
            "label": "Paused" if step == S.ACTIVE and status == S.PAUSED else step.label,
            "state": "done" if i < current else ("current" if i == current else "next"),
        }
        for i, step in enumerate(steps)
    ]


@workspace_access()
def subscription_detail(request, membership, pk):
    from recurring.models import Schedule

    subscription = get_object_or_404(
        Subscription.objects.select_related("customer", "plan", "template"),
        pk=pk,
        workspace=membership.workspace,
    )
    actions = [
        (name, label)
        for name, label in ACTIONS.items()
        if subscription.status in services.TRANSITIONS[name][0]
        and not services.transition_problem(subscription, name, timezone.localdate())
    ]
    schedule = Schedule.objects.filter(subscription=subscription).first()
    if schedule and schedule.mode == "automatic":
        actions = []
    return render(
        request,
        "billing/subscription_detail.html",
        {
            **context(membership),
            "subscription": subscription,
            "billing_schedule": schedule,
            "lines": subscription.lines.all(),
            "invoices": subscription.invoices.all(),
            "form": LineForm(workspace=membership.workspace),
            "actions": actions,
            "editable": subscription.status == Subscription.Status.DRAFT,
            "can_invoice": not schedule
            and subscription.status
            in [Subscription.Status.CONFIRMED, Subscription.Status.ACTIVE],
            "lifecycle": lifecycle(subscription.status),
        },
    )



@require_POST
@workspace_access()
def subscription_line(request, membership, pk, line_id=None):
    subscription = get_object_or_404(Subscription, pk=pk, workspace=membership.workspace)
    try:
        if line_id:
            services.remove_subscription_line(subscription, line_id)
            messages.success(request, "Line removed.")
        else:
            form = LineForm(request.POST, workspace=membership.workspace)
            if not form.is_valid():
                raise BillingError(" ".join(e for errors in form.errors.values() for e in errors))
            services.add_subscription_line(
                subscription, form.cleaned_data["item"], form.cleaned_data["quantity"]
            )
            messages.success(request, "Line added.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("subscription_detail", membership, pk))


@require_POST
@workspace_access()
def subscription_action(request, membership, pk):
    subscription = get_object_or_404(
        Subscription.objects.select_related("plan"), pk=pk, workspace=membership.workspace
    )
    action = request.POST.get("action", "")
    try:
        if action == "invoice":
            invoice = services.invoice_from_subscription(subscription, request.user)
            messages.success(request, "Draft invoice created. Review it, then issue it.")
            return redirect(url("invoice_detail", membership, invoice.pk))
        services.transition(subscription, action, request.user)
        messages.success(request, "Subscription updated.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("subscription_detail", membership, pk))


# Invoices

INVOICE_FILTERS = [
    ("", "All"),
    ("draft", "Drafts"),
    ("open", "Awaiting payment"),
    ("overdue", "Overdue"),
    ("paid", "Paid"),
    ("cancelled", "Cancelled"),
]


@workspace_access()
def invoices(request, membership):
    status = request.GET.get("status", "")
    today = timezone.localdate()
    rows = Invoice.objects.filter(workspace=membership.workspace).select_related("customer")
    filters = {
        "draft": Q(status=Invoice.Status.DRAFT),
        "open": Q(status=Invoice.Status.ISSUED),
        "overdue": Q(status=Invoice.Status.ISSUED, due_date__lt=today),
        "paid": Q(status=Invoice.Status.PAID),
        "cancelled": Q(status=Invoice.Status.CANCELLED),
    }
    if status in filters:
        rows = rows.filter(filters[status])
    else:
        status = ""
    return render(
        request,
        "billing/invoices.html",
        {
            **context(membership),
            "page": Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page")),
            "status": status,
            "query_string": query_string(status=status),
            "filters": INVOICE_FILTERS,
            "today": today,
            "summary": services.billing_summary(membership.workspace, today),
        },
    )


@workspace_access()
def invoice_edit(request, membership, pk=None):
    instance = pk and get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    if instance and instance.status != Invoice.Status.DRAFT:
        messages.error(request, "Issued invoices cannot be edited. Cancel and reissue instead.")
        return redirect(url("invoice_detail", membership, pk))
    form = InvoiceForm(
        request.POST or None,
        instance=instance or None,
        workspace=membership.workspace,
        initial={"customer": request.GET.get("customer")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if pk:
                    services.claim_draft(Invoice, pk, Invoice.Status.DRAFT)
                else:
                    form.instance.created_by = request.user
                invoice = form.save()
                services.recalculate_invoice(invoice)
                log(
                    membership.workspace,
                    request.user,
                    "Draft invoice updated" if pk else "Draft invoice created",
                    invoice.customer.name,
                )
        except BillingError as error:
            messages.error(request, str(error))
            return redirect(url("invoices", membership))
        messages.success(request, "Draft saved. Add lines, then issue the invoice.")
        return redirect(url("invoice_detail", membership, invoice.pk))
    return form_page(
        request,
        membership,
        form,
        "Edit draft invoice" if pk else "New invoice",
        "A one-time invoice in INR. Taxes, discounts, and totals are calculated by Mandali.",
        "Save draft",
        url("invoice_detail", membership, pk) if pk else url("invoices", membership),
    )


def invoice_context(membership, invoice):
    from recurring.models import Cycle

    automatic_cycle = (
        Cycle.objects.filter(invoice=invoice, schedule__mode="automatic")
        .select_related("schedule")
        .first()
    )
    lines = list(invoice.lines.all())
    problem = None
    if invoice.status == Invoice.Status.DRAFT and invoice.discount:
        problem = services.discount_problem(
            invoice.discount, lines, timezone.localdate()
        )
    payments = invoice.payments.select_related("recorded_by", "reversed_by")
    return {
        **context(membership),
        "invoice": invoice,
        "automatic_cycle": automatic_cycle,
        "lines": lines,
        "payments": payments,
        "credit_notes": invoice.credit_notes.select_related("created_by"),
        "credit_left": invoice.total - invoice.credited,
        "refunds": invoice.refunds.select_related("recorded_by"),
        "discount_problem": problem,
        "today": timezone.localdate(),
        "remove_url": (
            "invoice_line_remove" if invoice.status == Invoice.Status.DRAFT else ""
        ),
    }



@workspace_access()
def invoice_detail(request, membership, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer", "discount", "subscription"),
        pk=pk,
        workspace=membership.workspace,
    )
    data = invoice_context(membership, invoice)
    data.update(
        {
            "line_form": LineForm(workspace=membership.workspace),
            "payment_form": PaymentForm(initial={"received_on": timezone.localdate()}),
            "reason_form": ReasonForm(),
            "credit_form": CreditNoteForm(),
            "refund_form": RefundForm(initial={"refunded_on": timezone.localdate(), "amount": invoice.refund_due or None}),
        }
    )
    return render(request, "billing/invoice_detail.html", data)


@workspace_access()
def invoice_print(request, membership, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer"),
        pk=pk,
        workspace=membership.workspace,
    )
    return render(request, "billing/invoice_print.html", invoice_context(membership, invoice))


@require_POST
@workspace_access()
def invoice_line(request, membership, pk, line_id=None):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    try:
        if line_id:
            services.remove_invoice_line(invoice, line_id)
            messages.success(request, "Line removed.")
        else:
            form = LineForm(request.POST, workspace=membership.workspace)
            if not form.is_valid():
                raise BillingError(" ".join(e for errors in form.errors.values() for e in errors))
            services.add_invoice_line(
                invoice, form.cleaned_data["item"], form.cleaned_data["quantity"]
            )
            messages.success(request, "Line added.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, pk))


@require_POST
@workspace_access()
def invoice_issue(request, membership, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related("customer", "discount", "workspace"),
        pk=pk,
        workspace=membership.workspace,
    )
    try:
        services.issue_invoice(invoice, request.user)
        messages.success(request, f"Invoice {invoice.number} issued.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, pk))


@require_POST
@workspace_access()
def invoice_delete(request, membership, pk):
    with transaction.atomic():
        deleted, _ = Invoice.objects.filter(
            pk=pk, workspace=membership.workspace, status=Invoice.Status.DRAFT
        ).delete()
        if deleted:
            log(membership.workspace, request.user, "Draft invoice deleted")
    if not deleted:
        messages.error(request, "Only draft invoices can be deleted.")
        return redirect(url("invoice_detail", membership, pk))
    messages.success(request, "Draft invoice deleted.")
    return redirect(url("invoices", membership))


@require_POST
@workspace_access(admin=True)
def invoice_cancel(request, membership, pk):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    form = ReasonForm(request.POST)
    try:
        if not form.is_valid():
            raise BillingError("Give a reason for cancelling this invoice.")
        services.cancel_invoice(invoice, request.user, form.cleaned_data["reason"])
        messages.success(request, "Invoice cancelled. Its number is kept and not reused.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, pk))


@require_POST
@workspace_access()
def payment_record(request, membership, pk):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    form = PaymentForm(request.POST)
    if not form.is_valid():
        data = invoice_context(membership, invoice)
        data.update(
            {
                "line_form": LineForm(workspace=membership.workspace),
                "payment_form": form,
                "reason_form": ReasonForm(),
            }
        )
        return render(request, "billing/invoice_detail.html", data, status=400)
    try:
        payment = services.record_payment(invoice, request.user, **form.cleaned_data)
    except BillingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f"Payment of {inr(payment.amount)} recorded.")
        earlier = services.duplicate_reference(payment)
        if earlier:
            messages.warning(
                request,
                f"Reference {payment.reference} was also recorded on {earlier.invoice.label}. Check that this is not the same payment twice.",
            )
    return redirect(url("invoice_detail", membership, pk))


@require_POST
@workspace_access(admin=True)
def payment_reverse(request, membership, pk):
    payment = get_object_or_404(
        Payment.objects.select_related("invoice"), pk=pk, workspace=membership.workspace
    )
    form = ReasonForm(request.POST)
    try:
        if not form.is_valid():
            raise BillingError("Give a reason for reversing this payment.")
        services.reverse_payment(payment, request.user, form.cleaned_data["reason"])
        messages.success(request, "Payment reversed. The balance has been restored.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, payment.invoice_id))


@require_POST
@workspace_access(admin=True)
def credit_note_create(request, membership, pk):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    form = CreditNoteForm(request.POST)
    try:
        if not form.is_valid():
            raise BillingError("Enter a credit amount and a reason.")
        note = services.issue_credit_note(
            invoice, request.user, form.cleaned_data["amount"], form.cleaned_data["reason"]
        )
        invoice.refresh_from_db()
        messages.success(
            request,
            f"Credit note {note.number} issued."
            + (f" {inr(invoice.refund_due)} is now due back to the customer." if invoice.refund_due else ""),
        )
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, pk))


@require_POST
@workspace_access(admin=True)
def refund_create(request, membership, pk):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    form = RefundForm(request.POST)
    try:
        if not form.is_valid():
            raise BillingError(" ".join(e for errors in form.errors.values() for e in errors))
        services.record_refund(invoice, request.user, **form.cleaned_data)
        messages.success(request, f"Refund of {inr(form.cleaned_data['amount'])} recorded.")
    except BillingError as error:
        messages.error(request, str(error))
    return redirect(url("invoice_detail", membership, pk))


@workspace_access()
def payment_receipt(request, membership, pk, payment_id):
    invoice = get_object_or_404(Invoice, pk=pk, workspace=membership.workspace)
    payment = get_object_or_404(Payment, pk=payment_id, invoice=invoice)
    return render(
        request,
        "billing/receipt.html",
        {**context(membership), "invoice": invoice, "payment": payment, "back": url("invoice_detail", membership, pk)},
    )


@workspace_access()
def credit_note_print(request, membership, pk):
    note = get_object_or_404(CreditNote.objects.select_related("invoice"), pk=pk, workspace=membership.workspace)
    return render(
        request,
        "billing/credit_note.html",
        {**context(membership), "note": note, "invoice": note.invoice, "back": url("invoice_detail", membership, note.invoice_id)},
    )



@require_POST
@workspace_access()
def invoice_email(request, membership, pk):
    from django.conf import settings
    from django.core.mail import send_mail
    from core.security import count_attempt
    import logging
    invoice = get_object_or_404(Invoice.objects.select_related("customer"), pk=pk, workspace=membership.workspace)
    if invoice.status not in [Invoice.Status.ISSUED, Invoice.Status.PAID] or not invoice.customer_email:
        messages.error(request, "Issue the invoice and set the customer email before sending.")
    elif count_attempt("invoice-email", str(invoice.pk), 3600) > 5:
        messages.error(request, "This invoice has been emailed recently. Please try again later.")
    else:
        portal_url = settings.MANDALI_BASE_URL + reverse("portal_request")
        body = f"Invoice {invoice.number} from {invoice.business_name}\nTotal: INR {invoice.total}\nOutstanding: INR {invoice.balance}\nDue: {invoice.due_date}\n\nView and print your invoice: {portal_url}\nRequest a secure link using this email address."
        try:
            send_mail(f"Invoice {invoice.number} from {invoice.business_name}", body, settings.DEFAULT_FROM_EMAIL, [invoice.customer_email], fail_silently=False)
        except Exception:
            logging.getLogger(__name__).error("Invoice email delivery failed.")
            messages.error(request, "Email could not be sent. Please try again later.")
        else:
            log(membership.workspace, request.user, "Invoice emailed", invoice.number)
            messages.success(request, "Invoice email accepted for delivery.")
    return redirect("invoice_detail", workspace_id=membership.workspace.pk, pk=invoice.pk)
