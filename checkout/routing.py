"""Gateway eligibility, ranking, and recorded routing decisions.

Rules:
- A gateway is considered only when it passes every eligibility check for
  this payment. An incompatible gateway is never chosen.
- Ranking uses only the business's own profile figures and Mandali's own
  measured attempts. A missing figure is unknown, ranked after known ones,
  and never estimated.
- Ties break deterministically: the default gateway, then a fixed order.
- A manual override pins one gateway; if it is ineligible, nothing is routed.
- Fallback to another gateway happens only in automatic mode, and only after
  an order was definitively rejected (no money can have moved). An uncertain
  order is reconciled first and never replaced automatically.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from django.db.models import Count, Q
from django.utils import timezone
from billing.templatetags.billing import inr
from .crypto import CheckoutError
from .gateways import adapter
from .models import (
    GatewayAccount,
    GatewayProfile,
    InvoiceRoute,
    PaymentAttempt,
    Provider,
    RoutingDecision,
    RoutingPolicy,
)

ORDER = [Provider.RAZORPAY, Provider.CASHFREE]
SUCCESS_WINDOW = timedelta(days=30)
SUCCESS_MIN_SAMPLE = 20
# Attempts still open after this long count as unsuccessful in the measure.
SUCCESS_SETTLE = timedelta(hours=1)
NEEDS_PHONE = {Provider.CASHFREE}
SUPPORTED_CURRENCIES = {"INR"}


def policy_for(workspace):
    return RoutingPolicy.objects.filter(workspace=workspace).first()


def profiles(workspace):
    return {p.provider: p for p in GatewayProfile.objects.filter(workspace=workspace)}


def success_rates(workspace, now=None):
    """Share of prepared orders that were paid, per provider, over 30 days."""
    now = now or timezone.now()
    rows = (
        PaymentAttempt.objects.filter(
            workspace=workspace,
            created_at__gte=now - SUCCESS_WINDOW,
            created_at__lte=now - SUCCESS_SETTLE,
        )
        .exclude(provider_order_id="")
        .values("gateway__provider")
        .annotate(total=Count("id"), paid=Count("id", filter=Q(status="paid")))
    )
    rates = {}
    for row in rows:
        if row["total"] >= SUCCESS_MIN_SAMPLE:
            rates[row["gateway__provider"]] = {
                "rate": (Decimal(row["paid"]) / row["total"] * 100).quantize(Decimal("0.1")),
                "sample": row["total"],
            }
    return rates


def estimated_fee(profile, amount):
    if profile is None or profile.fee_percent is None or profile.fee_fixed is None:
        return None
    fee = amount * profile.fee_percent / 100 + profile.fee_fixed
    return fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def assess(workspace, provider, amount, currency, phone, accounts, profile, rates):
    """Return one candidate: eligibility reasons and the figures used to rank."""
    reasons = []
    account = accounts.get(provider)
    if account is None:
        reasons.append("Not connected")
    else:
        try:
            adapter(account)
        except CheckoutError:
            reasons.append("Saved credentials cannot be used")
    if currency not in SUPPORTED_CURRENCIES:
        reasons.append(f"{currency} is not supported")
    if profile and profile.min_amount is not None and amount < profile.min_amount:
        reasons.append(f"Below this account's minimum of {inr(profile.min_amount)}")
    if profile and profile.max_amount is not None and amount > profile.max_amount:
        reasons.append(f"Above this account's maximum of {inr(profile.max_amount)}")
    if provider in NEEDS_PHONE and not phone:
        reasons.append("Needs the customer's phone number")
    fee = estimated_fee(profile, amount)
    rate = rates.get(provider)
    return {
        "provider": provider,
        "label": Provider(provider).label,
        "eligible": not reasons,
        "reasons": reasons,
        "fee": str(fee) if fee is not None else None,
        "settlement_days": profile.settlement_days if profile else None,
        "success_rate": str(rate["rate"]) if rate else None,
        "success_sample": rate["sample"] if rate else None,
        "account": str(account.pk) if account else None,
    }


def sort_key(candidate, priority, default):
    value = {
        RoutingPolicy.Priority.FEES: candidate["fee"] and Decimal(candidate["fee"]),
        RoutingPolicy.Priority.SETTLEMENT: candidate["settlement_days"],
        RoutingPolicy.Priority.SUCCESS: candidate["success_rate"] and -Decimal(candidate["success_rate"]),
    }[priority]
    return (
        value is None,
        value if value is not None else 0,
        candidate["provider"] != default,
        ORDER.index(candidate["provider"]),
    )


def figure(candidate, priority):
    if priority == RoutingPolicy.Priority.FEES:
        return f"estimated fee {inr(candidate['fee'])}" if candidate["fee"] else "fee unknown"
    if priority == RoutingPolicy.Priority.SETTLEMENT:
        days = candidate["settlement_days"]
        return f"settles in {days} day{'s' if days != 1 else ''}" if days is not None else "settlement time unknown"
    if candidate["success_rate"]:
        return f"{candidate['success_rate']}% paid of {candidate['success_sample']} recent orders"
    return f"success unknown (fewer than {SUCCESS_MIN_SAMPLE} recent orders)"


def decide(workspace, amount, currency="INR", phone="", override=None, rejected=()):
    """Choose a provider for one payment. Pure apart from reading configuration."""
    policy = policy_for(workspace)
    mode = policy.mode if policy else RoutingPolicy.Mode.DEFAULT
    default = policy.default_provider if policy else ""
    priority = policy.priority if policy else RoutingPolicy.Priority.FEES
    accounts = {
        a.provider: a for a in GatewayAccount.objects.filter(workspace=workspace, is_active=True)
    }
    found = profiles(workspace)
    rates = success_rates(workspace)
    candidates = [
        assess(workspace, p, amount, currency, phone, accounts, found.get(p), rates) for p in ORDER
    ]
    by_provider = {c["provider"]: c for c in candidates}
    eligible = [c for c in candidates if c["eligible"]]
    snapshot = {"mode": mode, "default": default, "priority": priority}
    skipped = "; ".join(
        f"{c['label']} skipped: {', '.join(c['reasons']).lower()}" for c in candidates if not c["eligible"]
    )

    def result(chosen, basis, explanation):
        if skipped and basis != RoutingDecision.Basis.NONE:
            explanation += f". {skipped}"
        return {
            "provider": chosen["provider"] if chosen else "",
            "basis": basis,
            "explanation": explanation[:400],
            "candidates": candidates,
            "policy": snapshot,
            "account": accounts.get(chosen["provider"]) if chosen else None,
        }

    if override:
        chosen = by_provider[override]
        if chosen["eligible"]:
            return result(chosen, RoutingDecision.Basis.OVERRIDE, f"{chosen['label']} set manually for this invoice")
        return result(
            None,
            RoutingDecision.Basis.NONE,
            f"{chosen['label']} is set manually for this invoice but cannot take this payment: {', '.join(chosen['reasons']).lower()}",
        )
    if mode == RoutingPolicy.Mode.AUTOMATIC:
        pool = [c for c in eligible if c["provider"] not in rejected]
        if not pool:
            return result(None, RoutingDecision.Basis.NONE, "No connected gateway is eligible for this payment")
        pool.sort(key=lambda c: sort_key(c, priority, default))
        chosen = pool[0]
        if rejected:
            names = ", ".join(Provider(p).label for p in rejected)
            return result(chosen, RoutingDecision.Basis.FALLBACK, f"{chosen['label']} chosen after {names} rejected the order")
        if len(pool) == 1:
            return result(chosen, RoutingDecision.Basis.ONLY, f"{chosen['label']} is the only eligible gateway")
        others = ", ".join(f"{c['label']}: {figure(c, priority)}" for c in pool[1:])
        label = RoutingPolicy.Priority(priority).label.lower()
        return result(
            chosen,
            RoutingDecision.Basis.AUTOMATIC,
            f"{chosen['label']} chosen automatically for {label} ({figure(chosen, priority)}; {others})",
        )
    if default:
        chosen = by_provider[default]
        if chosen["eligible"]:
            return result(chosen, RoutingDecision.Basis.DEFAULT, f"{chosen['label']} is the default gateway")
        return result(
            None,
            RoutingDecision.Basis.NONE,
            f"The default gateway, {chosen['label']}, cannot take this payment: {', '.join(chosen['reasons']).lower()}. Automatic selection is off",
        )
    if len(eligible) == 1:
        return result(eligible[0], RoutingDecision.Basis.ONLY, f"{eligible[0]['label']} is the only eligible gateway")
    if not eligible:
        return result(None, RoutingDecision.Basis.NONE, "No connected gateway is eligible for this payment")
    return result(
        None,
        RoutingDecision.Basis.NONE,
        "Several gateways are connected but no default is set. Choose a default or turn on automatic selection",
    )


def route(invoice, phone=""):
    """Decide and record the gateway for a new payment attempt on this invoice.

    Called inside the transaction that reserves the attempt.
    """
    override = InvoiceRoute.objects.filter(invoice=invoice).values_list("provider", flat=True).first()
    rejected = tuple(
        PaymentAttempt.objects.filter(invoice=invoice, status="rejected")
        .values_list("gateway__provider", flat=True)
        .distinct()
    )
    phone = phone or invoice.customer.phone
    outcome = decide(
        invoice.workspace,
        invoice.balance,
        invoice.currency,
        phone,
        override=override,
        rejected=rejected if not override else (),
    )
    decision = RoutingDecision.objects.create(
        workspace=invoice.workspace,
        invoice=invoice,
        provider=outcome["provider"],
        basis=outcome["basis"],
        explanation=outcome["explanation"],
        candidates=outcome["candidates"],
        policy=outcome["policy"],
    )
    return decision, outcome["account"]


def link_attempt(decision, attempt):
    RoutingDecision.objects.filter(pk=decision.pk).update(attempt=attempt)


def recommend(workspace, needs):
    """Rank gateways against what a business says it needs.

    needs: methods (list), recurring (bool), amount (Decimal or None), priority.
    Uses only the business's own profile figures; missing figures stay unknown.
    """
    found = profiles(workspace)
    accounts = {a.provider for a in GatewayAccount.objects.filter(workspace=workspace, is_active=True)}
    rates = success_rates(workspace)
    amount = needs.get("amount")
    rows = []
    for provider in ORDER:
        profile = found.get(provider)
        gaps, misses = [], []
        methods = set(profile.methods) if profile else set()
        if not profile:
            gaps.append("No account details entered yet")
        missing = [m for m in needs["methods"] if m not in methods]
        if profile and missing:
            labels = dict(GatewayProfile.METHODS)
            misses.append("Not enabled for " + ", ".join(labels[m] for m in missing))
        if needs["recurring"]:
            if not profile or profile.recurring == GatewayProfile.Recurring.UNKNOWN:
                gaps.append("Recurring support not confirmed")
            elif profile.recurring == GatewayProfile.Recurring.NO:
                misses.append("No recurring support")
        if amount is not None and profile:
            if profile.min_amount is not None and amount < profile.min_amount:
                misses.append(f"Minimum is {inr(profile.min_amount)}")
            if profile.max_amount is not None and amount > profile.max_amount:
                misses.append(f"Maximum is {inr(profile.max_amount)}")
        fee = estimated_fee(profile, amount) if amount is not None else None
        rate = rates.get(provider)
        rows.append(
            {
                "provider": provider,
                "label": Provider(provider).label,
                "connected": provider in accounts,
                "fits": not misses and not gaps,
                "misses": misses,
                "gaps": gaps,
                "fee": str(fee) if fee is not None else None,
                "settlement_days": profile.settlement_days if profile else None,
                "success_rate": str(rate["rate"]) if rate else None,
                "success_sample": rate["sample"] if rate else None,
            }
        )
    priority = needs["priority"]
    rows.sort(key=lambda r: (bool(r["misses"]), bool(r["gaps"]), *sort_key(r, priority, "")[:2], ORDER.index(r["provider"])))
    for row in rows:
        row["figure"] = figure(row, priority)
    return rows
