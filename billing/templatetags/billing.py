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
