"""Small checks for the workspace routing and navigation contract."""
from types import SimpleNamespace
from uuid import UUID

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse

from core.models import User, Workspace, Membership
from core.templatetags.navigation import section_for_route
from billing.models import Customer, Subscription

WORKSPACE_ID = UUID("12345678-1234-5678-1234-567812345678")


class NavigationTests(SimpleTestCase):
    def test_related_pages_select_exactly_one_owner_section(self):
        for route, section, args in [
            ("recurring_setup", "automation", [WORKSPACE_ID, 1]),
            ("campaign_spend", "analytics", [WORKSPACE_ID]),
            ("invoice_bank_import", "invoices", [WORKSPACE_ID]),
            ("payment_reverse", "invoices", [WORKSPACE_ID, 1]),
            ("credit_note_print", "invoices", [WORKSPACE_ID, 1]),
            ("template_detail", "catalog", [WORKSPACE_ID, 1]),
            ("invite", "team", [WORKSPACE_ID]),
            ("checkout_routing", "payment_links", [WORKSPACE_ID]),
            ("integration_run", "integrations", [WORKSPACE_ID, 1]),
        ]:
            with self.subTest(route=route):
                request = RequestFactory().get(reverse(route, args=args))
                request.resolver_match = resolve(request.path)
                html = render_to_string("core/navigation.html", {
                    "request": request,
                    "workspace": SimpleNamespace(pk=WORKSPACE_ID),
                    "is_admin": True,
                })
                self.assertEqual(html.count('aria-current="page"'), 1)
                self.assertEqual(section_for_route(route), section)

    def test_staff_navigation_still_omits_admin_destinations(self):
        request = RequestFactory().get(reverse("dashboard", args=[WORKSPACE_ID]))
        request.resolver_match = resolve(request.path)
        html = render_to_string("core/navigation.html", {
            "request": request, "workspace": SimpleNamespace(pk=WORKSPACE_ID), "is_admin": False,
        })
        for destination in ("analytics", "integration_keys", "workspace_settings"):
            self.assertNotIn(reverse(destination, args=[WORKSPACE_ID]), html)
        self.assertIn(reverse("reports", args=[WORKSPACE_ID]), html)

    def test_moved_core_urls_keep_their_names_and_paths(self):
        expected = [
            ("login", [], "/login/"), ("signup", [], "/signup/"),
            ("health_live", [], "/health/live/"), ("health_ready", [], "/health/ready/"),
            ("password_reset", [], "/password/reset/"),
            ("password_reset_done", [], "/password/reset/sent/"),
            ("password_reset_complete", [], "/password/reset/complete/"),
            ("password_reset_confirm", ["MQ", "token"], "/password/reset/MQ/token/"),
            ("accept_invite", ["token"], "/join/token/"),
            ("dashboard", [WORKSPACE_ID], f"/w/{WORKSPACE_ID}/"),
            ("team", [WORKSPACE_ID], f"/w/{WORKSPACE_ID}/team/"),
        ]
        for name, args, path in expected:
            with self.subTest(name=name):
                self.assertEqual(reverse(name, args=args), path)
                self.assertEqual(resolve(path).url_name, name)

    def test_unrelated_or_missing_route_has_no_sidebar_selection(self):
        self.assertEqual(section_for_route(None), "")
        self.assertEqual(section_for_route("portal_home"), "")


class FormReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("polish@example.invalid", "Local-test!42")
        cls.workspace = Workspace.objects.create(name="Example", legal_name="Example", billing_email=cls.owner.email, address="India")
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role="admin")
        customer = Customer.objects.create(workspace=cls.workspace, name="Example customer")
        cls.subscription = Subscription.objects.create(workspace=cls.workspace, customer=customer, number="SUB-1", created_by=cls.owner)

    def test_checkout_create_goes_back_to_payment_links(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("checkout_link_create", args=[self.workspace.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cancel_url"], reverse("checkout_links", args=[self.workspace.pk]))

    def test_automation_forms_go_back_to_their_subscription(self):
        self.client.force_login(self.owner)
        expected = reverse("subscription_detail", args=[self.workspace.pk, self.subscription.pk])
        for name in ("recurring_setup", "recurring_renew"):
            response = self.client.get(reverse(name, args=[self.workspace.pk, self.subscription.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["cancel_url"], expected)
