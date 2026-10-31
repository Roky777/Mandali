"""Select one sidebar section for each workspace route.

Match route families rather than substrings: recurring_setup belongs to
Automation, while payment_reverse and credit_note_print belong to Invoices.
This is presentation only; authorization remains in the workspace views.
"""
from django import template

register = template.Library()

SECTION_PREFIXES = (
    ("recurring_", "automation"),
    ("checkout_", "payment_links"),
    ("integration_", "integrations"),
    ("reports", "reports"),
    ("campaign_", "analytics"),
    ("catalog", "catalog"),
    ("product_", "catalog"),
    ("variant_", "catalog"),
    ("template_", "catalog"),
    ("subscription", "subscriptions"),
    ("customer", "customers"),
    ("invoice", "invoices"),
    ("payment_", "invoices"),
    ("credit_note_", "invoices"),
    ("refund_", "invoices"),
)
SECTION_ROUTES = {
    "dashboard": "overview",
    "analytics": "analytics",
    "team": "team",
    "invite": "team",
    "revoke_invite": "team",
    "remove_staff": "team",
    "workspace_settings": "settings",
}


def section_for_route(route_name):
    route_name = route_name or ""
    if route_name in SECTION_ROUTES:
        return SECTION_ROUTES[route_name]
    return next((section for prefix, section in SECTION_PREFIXES if route_name.startswith(prefix)), "")


@register.simple_tag(takes_context=True)
def current_section(context):
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    return section_for_route(getattr(match, "url_name", None))
