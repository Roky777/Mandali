from decimal import ROUND_HALF_UP, Decimal
from django import template

register = template.Library()


@register.filter
def inr(value):
    """Format an amount in rupees with Indian digit grouping: ₹1,23,456.00."""
    if value in (None, ""):
        return ""
    amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return f"{sign}₹{whole}.{fraction}"


@register.filter
def rupees_from_paise(value):
    return inr(Decimal(value) / 100)


@register.filter
def compact_inr(value):
    amount = Decimal(value or 0)
    for threshold, label in [(Decimal("10000000"), "Cr"), (Decimal("100000"), "L"), (Decimal("1000"), "K")]:
        if abs(amount) >= threshold:
            return f"₹{amount/threshold:.1f}{label}"
    return f"₹{amount:.0f}"


@register.simple_tag
def gst_split(invoice):
    """Split invoice tax into CGST and SGST within one state, IGST across states.

    The supplier state is the first two digits of the business GSTIN; the
    place of supply is taken from the customer GSTIN when there is one.
    """
    tax = Decimal(invoice.tax_total or 0)
    seller = (invoice.business_gstin or "")[:2]
    buyer = (invoice.customer_gstin or "")[:2]
    if seller and buyer and seller != buyer:
        return {"igst": tax}
    half = (tax / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"cgst": half, "sgst": tax - half}


@register.filter
def margin(product):
    """Profit per unit and margin percentage from sales and cost price."""
    profit = product.sales_price - product.cost_price
    percent = (profit / product.sales_price * 100).quantize(Decimal("0.1")) if product.sales_price else Decimal("0")
    return {"profit": profit, "percent": percent}

