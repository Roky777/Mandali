import base64
import hashlib
import hmac
import io
import json
import urllib.error
from decimal import Decimal
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import services as billing
from billing.forms import PaymentForm
from billing.models import Customer, Invoice, Payment, Product
from core.models import Membership, User, Workspace
from . import routing, services
from .forms import CashfreeForm, PolicyForm, ProfileForm
from .gateways import CashfreeTest, GatewayError, GatewayRejected, RazorpayTest, paise_from_rupees
from .models import (
    CheckoutSession,
    GatewayAccount,
    GatewayProfile,
    InvoiceRoute,
    PaymentAttempt,
    Provider,
    RoutingDecision,
    RoutingPolicy,
)

D = Decimal
CF_SECRET = "cashfree-sandbox-secret-never-real"


@override_settings(
    PAYMENT_ENCRYPTION_KEYS=Fernet.generate_key().decode(),
    MANDALI_BASE_URL="http://testserver",
)
class RoutingTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", "Local-test!Owner42", first_name="Owner")
        cls.staff = User.objects.create_user("staff@example.com", "Local-test!Staff42")
        cls.outsider = User.objects.create_user("other@example.com", "Local-test!Other42")
        cls.workspace = Workspace.objects.create(
            name="Example Studio", legal_name="Example Studio LLP",
            billing_email="accounts@example.com", address="Example address",
        )
        cls.foreign = Workspace.objects.create(
            name="Other Studio", legal_name="Other LLP", billing_email="o@example.com", address="x"
        )
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.workspace, user=cls.staff, role="staff")
        Membership.objects.create(workspace=cls.foreign, user=cls.outsider, role="admin")
        cls.product = Product.objects.create(workspace=cls.workspace, name="Service", sales_price=1000)
        cls.customer = Customer.objects.create(
            workspace=cls.workspace, name="Buyer", email="buyer@example.com", phone="+91 98765 43210"
        )
        cls.no_phone = Customer.objects.create(workspace=cls.workspace, name="No phone", email="np@example.com")

    def setUp(self):
        self.razorpay = services.configure_gateway(
            self.workspace, self.owner,
            {"key_id": "rzp_test_example123", "key_secret": "rzp-secret-never-real", "webhook_secret": "rzp-webhook-never-real"},
        )
        self.cashfree = services.configure_gateway(
            self.workspace, self.owner, {"key_id": "TEST1234abcd", "key_secret": CF_SECRET}, Provider.CASHFREE
        )

    def invoice(self, customer=None, quantity=1):
        invoice = Invoice.objects.create(
            workspace=self.workspace, customer=customer or self.customer, created_by=self.owner, is_sandbox=True
        )
        billing.add_invoice_line(invoice, {"product": self.product}, quantity)
        billing.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        return invoice

    def policy(self, **values):
        RoutingPolicy.objects.update_or_create(
            workspace=self.workspace, defaults={"updated_by": self.owner, **values}
        )

    def profile(self, provider, **values):
        GatewayProfile.objects.update_or_create(
            workspace=self.workspace, provider=provider, defaults={"updated_by": self.owner, **values}
        )

    def decide(self, amount="1000", phone="9876543210", **kwargs):
        return routing.decide(self.workspace, D(amount), "INR", phone, **kwargs)


class DecisionTests(RoutingTestCase):
    def test_single_gateway_without_policy_keeps_part_three_behaviour(self):
        GatewayAccount.objects.filter(pk=self.cashfree.pk).update(is_active=False)
        outcome = self.decide()
        self.assertEqual((outcome["provider"], outcome["basis"]), ("razorpay", "only"))
        self.assertIn("Cashfree Sandbox skipped: not connected", outcome["explanation"])

    def test_two_gateways_need_a_default_or_automatic_mode(self):
        outcome = self.decide()
        self.assertEqual((outcome["provider"], outcome["basis"]), ("", "none"))
        self.policy(default_provider="cashfree")
        self.assertEqual(self.decide()["provider"], "cashfree")

    def test_default_mode_never_switches_to_another_gateway(self):
        self.policy(default_provider="cashfree")
        outcome = self.decide(phone="")
        self.assertEqual((outcome["provider"], outcome["basis"]), ("", "none"))
        self.assertIn("needs the customer's phone number", outcome["explanation"])

    def test_automatic_ranks_by_entered_fees_and_unknown_ranks_last(self):
        self.policy(mode="automatic", priority="fees", default_provider="razorpay")
        self.profile("razorpay", fee_percent=D("2"), fee_fixed=D("0"))
        self.profile("cashfree", fee_percent=D("1.75"), fee_fixed=D("3"))
        outcome = self.decide("2000")
        self.assertEqual((outcome["provider"], outcome["basis"]), ("cashfree", "automatic"))
        self.assertIn("₹38.00", outcome["explanation"])
        self.assertIn("₹40.00", outcome["explanation"])
        self.assertEqual(self.decide("100")["provider"], "razorpay")
        GatewayProfile.objects.filter(provider="cashfree").update(fee_percent=None, fee_fixed=None)
        self.assertEqual(self.decide("2000")["provider"], "razorpay")

    def test_ties_go_to_the_default_and_are_repeatable(self):
        self.policy(mode="automatic", priority="settlement", default_provider="cashfree")
        self.profile("razorpay", settlement_days=2)
        self.profile("cashfree", settlement_days=2)
        results = {self.decide()["provider"] for _ in range(5)}
        self.assertEqual(results, {"cashfree"})
        self.policy(mode="automatic", priority="settlement", default_provider="")
        self.assertEqual(self.decide()["provider"], "razorpay")

    def test_incompatible_gateway_is_never_selected(self):
        self.policy(mode="automatic", priority="fees")
        self.profile("razorpay", fee_percent=D("5"), fee_fixed=D("0"), max_amount=D("500"))
        self.profile("cashfree", fee_percent=D("9"), fee_fixed=D("0"))
        outcome = self.decide("1000")
        self.assertEqual((outcome["provider"], outcome["basis"]), ("cashfree", "only"))
        skipped = next(c for c in outcome["candidates"] if c["provider"] == "razorpay")
        self.assertFalse(skipped["eligible"])
        self.assertIn("Above this account's maximum of ₹500.00", skipped["reasons"])
        self.assertEqual(self.decide("1000", phone="")["provider"], "")

    def test_broken_credentials_are_ineligible(self):
        self.policy(mode="automatic")
        GatewayAccount.objects.filter(pk=self.razorpay.pk).update(secrets_ciphertext="corrupt")
        outcome = self.decide()
        self.assertEqual(outcome["provider"], "cashfree")
        self.assertIn("saved credentials cannot be used", outcome["explanation"])

    def test_success_rate_needs_a_minimum_sample(self):
        self.policy(mode="automatic", priority="success", default_provider="cashfree")
        long_ago = timezone.now() - timezone.timedelta(days=2)
        for index in range(routing.SUCCESS_MIN_SAMPLE):
            invoice = self.invoice()
            attempt = PaymentAttempt.objects.create(
                workspace=self.workspace, invoice=invoice, gateway=self.razorpay, key_id=self.razorpay.key_id,
                amount_paise=100, provider_order_id=f"order_r{index}", status="paid" if index % 2 else "rejected",
            )
            PaymentAttempt.objects.filter(pk=attempt.pk).update(created_at=long_ago)
        rates = routing.success_rates(self.workspace)
        self.assertEqual(rates["razorpay"]["rate"], D("50.0"))
        self.assertNotIn("cashfree", rates)
        outcome = self.decide()
        self.assertEqual(outcome["provider"], "razorpay")
        self.assertIn("success unknown", outcome["explanation"])

    def test_override_pins_a_gateway_and_never_falls_back(self):
        self.policy(mode="automatic", default_provider="razorpay")
        self.assertEqual(self.decide(override="cashfree")["basis"], "override")
        outcome = self.decide(phone="", override="cashfree")
        self.assertEqual((outcome["provider"], outcome["basis"]), ("", "none"))

    def test_fallback_only_after_a_definitive_rejection(self):
        self.policy(mode="automatic", default_provider="razorpay")
        outcome = self.decide(rejected=("razorpay",))
        self.assertEqual((outcome["provider"], outcome["basis"]), ("cashfree", "fallback"))
        self.policy(mode="default", default_provider="razorpay")
        self.assertEqual(self.decide(rejected=("razorpay",))["provider"], "razorpay")


def cashfree_order(attempt, session_id="session_abc"):
    return {
        "id": "order_" + attempt.pk.hex,
        "amount": attempt.amount_paise,
        "currency": "INR",
        "receipt": attempt.pk.hex,
        "notes": {"mandali_attempt": str(attempt.pk)},
        "client_token": session_id,
    }


def cashfree_payment(attempt, status="captured"):
    return {
        "id": "pay_555001",
        "order_id": attempt.provider_order_id,
        "amount": attempt.amount_paise,
        "currency": "INR",
        "status": status,
        "captured": status == "captured",
        "amount_refunded": 0,
    }


class CheckoutRoutingTests(RoutingTestCase):
    def session(self, invoice, phone=""):
        link = services.create_link(
            self.workspace, self.owner,
            {"title": "Pay", "invoice": invoice, "product": None, "plan": None, "quantity": 1, "valid_days": 7},
        )
        return CheckoutSession.objects.create(
            link=link, invoice=invoice, creation_digest=str(invoice.pk) + phone, buyer_phone=phone,
            expires_at=timezone.now() + timezone.timedelta(hours=2),
        )

    def test_begin_records_the_decision_and_offers_cashfree_checkout(self):
        self.policy(default_provider="cashfree")
        session = self.session(self.invoice())
        with patch.object(CashfreeTest, "create_order", autospec=True, side_effect=lambda gateway, attempt: cashfree_order(attempt)):
            attempt = services.begin(session)
        self.assertEqual((attempt.gateway.provider, attempt.status, attempt.client_token), ("cashfree", "ready", "session_abc"))
        decision = RoutingDecision.objects.get()
        self.assertEqual((decision.attempt, decision.basis), (attempt, "default"))
        response = self.client.get(services.session_url(session))
        self.assertContains(response, "session_abc")
        self.assertContains(response, "sdk.cashfree.com")
        self.assertIn("https://sdk.cashfree.com", response["Content-Security-Policy"])
        self.assertNotIn("razorpay", response["Content-Security-Policy"])

    def test_no_eligible_gateway_reserves_nothing(self):
        self.policy(default_provider="cashfree")
        session = self.session(self.invoice(customer=self.no_phone))
        with self.assertRaisesMessage(services.CheckoutError, "no gateway that can accept"):
            services.begin(session)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertEqual(RoutingDecision.objects.get().basis, "none")

    def test_rejected_order_falls_back_in_automatic_mode(self):
        self.policy(mode="automatic", default_provider="razorpay")
        session = self.session(self.invoice())
        with patch.object(RazorpayTest, "create_order", side_effect=GatewayRejected("Rejected")):
            first = services.begin(session)
        self.assertEqual((first.gateway.provider, first.status), ("razorpay", "rejected"))
        with patch.object(CashfreeTest, "create_order", autospec=True, side_effect=lambda gateway, attempt: cashfree_order(attempt)):
            second = services.begin(session)
        self.assertEqual(second.gateway.provider, "cashfree")
        self.assertEqual(second.routing.basis, "fallback")

    def test_uncertain_order_is_never_replaced(self):
        self.policy(mode="automatic", default_provider="razorpay")
        session = self.session(self.invoice())
        with patch.object(RazorpayTest, "create_order", side_effect=GatewayError("Timeout")):
            first = services.begin(session)
        self.assertEqual(first.status, "unknown")
        with patch.object(CashfreeTest, "create_order") as create:
            again = services.begin(session)
        create.assert_not_called()
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_manual_override_routes_the_next_attempt(self):
        self.policy(default_provider="razorpay")
        invoice = self.invoice()
        InvoiceRoute.objects.create(invoice=invoice, provider="cashfree", reason="Customer asked", set_by=self.owner)
        with patch.object(CashfreeTest, "create_order", autospec=True, side_effect=lambda gateway, attempt: cashfree_order(attempt)):
            attempt = services.begin(self.session(invoice))
        self.assertEqual((attempt.gateway.provider, attempt.routing.basis), ("cashfree", "override"))

    def ready_cashfree(self):
        self.policy(default_provider="cashfree")
        invoice = self.invoice()
        with patch.object(CashfreeTest, "create_order", autospec=True, side_effect=lambda gateway, attempt: cashfree_order(attempt)):
            attempt = services.begin(self.session(invoice))
        return invoice, attempt

    def signed_post(self, account, body, secret=CF_SECRET, timestamp="1761900000000"):
        signature = base64.b64encode(
            hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
        ).decode()
        return self.client.post(
            reverse("checkout_webhook_cashfree", args=[account.pk]), body, content_type="application/json",
            HTTP_X_WEBHOOK_TIMESTAMP=timestamp, HTTP_X_WEBHOOK_SIGNATURE=signature,
        )

    def webhook_body(self, attempt):
        return json.dumps({
            "type": "PAYMENT_SUCCESS_WEBHOOK",
            "data": {"order": {"order_id": attempt.provider_order_id}, "payment": {"cf_payment_id": 555001}},
        }).encode()

    def test_cashfree_webhook_settles_once(self):
        invoice, attempt = self.ready_cashfree()
        body = self.webhook_body(attempt)
        with patch.object(CashfreeTest, "fetch_payment", return_value=cashfree_payment(attempt)) as fetch:
            response = self.signed_post(self.cashfree, body)
            replay = self.signed_post(self.cashfree, body)
        self.assertEqual(response.json()["status"], "paid")
        self.assertEqual(replay.json()["status"], "duplicate")
        self.assertEqual(fetch.call_args.kwargs["order_id"], attempt.provider_order_id)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        payment = Payment.objects.get(invoice=invoice)
        self.assertEqual((payment.method, payment.reference), (Payment.Method.CASHFREE_TEST, "pay_555001"))
        self.assertIn("Verified by Cashfree Sandbox", self.admin_invoice_page(invoice))

    def admin_invoice_page(self, invoice):
        self.client.force_login(self.owner)
        return self.client.get(reverse("invoice_detail", args=[self.workspace.pk, invoice.pk])).content.decode()

    def test_cashfree_webhook_rejects_bad_signatures_and_wrong_routes(self):
        _, attempt = self.ready_cashfree()
        body = self.webhook_body(attempt)
        with patch.object(CashfreeTest, "fetch_payment") as fetch:
            self.assertEqual(self.signed_post(self.cashfree, body, secret="wrong").status_code, 400)
            fetch.assert_not_called()
        self.assertEqual(self.signed_post(self.razorpay, body).status_code, 404)

    def test_return_check_reconciles_through_the_api(self):
        invoice, attempt = self.ready_cashfree()
        with patch.object(CashfreeTest, "order_payments", return_value=[cashfree_payment(attempt)]), patch.object(
            CashfreeTest, "fetch_payment", return_value=cashfree_payment(attempt)
        ):
            services.reconcile(attempt)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    def test_failed_cashfree_payment_records_nothing(self):
        invoice, attempt = self.ready_cashfree()
        with patch.object(CashfreeTest, "order_payments", return_value=[cashfree_payment(attempt, "failed")]):
            services.reconcile(attempt)
        self.assertFalse(Payment.objects.filter(invoice=invoice).exists())

    def test_gateway_payments_stay_out_of_the_manual_form(self):
        choices = dict(PaymentForm().fields["method"].choices)
        self.assertNotIn(Payment.Method.CASHFREE_TEST, choices)
        self.assertNotIn(Payment.Method.RAZORPAY_TEST, choices)


class CashfreeAdapterTests(RoutingTestCase):
    def attempt(self):
        invoice = self.invoice()
        return PaymentAttempt.objects.create(
            workspace=self.workspace, invoice=invoice, gateway=self.cashfree, key_id=self.cashfree.key_id,
            amount_paise=118000,
        )

    def test_order_request_and_normalised_response(self):
        attempt = self.attempt()
        raw = {
            "order_id": "order_" + attempt.pk.hex, "order_amount": 1180.0, "order_currency": "INR",
            "order_tags": {"mandali_attempt": str(attempt.pk)}, "payment_session_id": "session_x",
        }
        gateway = CashfreeTest(self.cashfree)
        with patch.object(gateway, "request", return_value=raw) as send:
            order = gateway.create_order(attempt)
        method, path, payload = send.call_args.args
        self.assertEqual((method, path), ("POST", "orders"))
        self.assertEqual(payload["order_id"], "order_" + attempt.pk.hex)
        self.assertEqual(payload["order_amount"], 1180.0)
        self.assertEqual(payload["customer_details"]["customer_phone"], "+919876543210")
        self.assertEqual(services.validate_order(attempt, order), "order_" + attempt.pk.hex)
        self.assertEqual(order["client_token"], "session_x")

    def test_missing_phone_is_a_definitive_rejection_before_any_request(self):
        attempt = self.attempt()
        Customer.objects.filter(pk=self.customer.pk).update(phone="")
        attempt.invoice.customer.refresh_from_db()
        gateway = CashfreeTest(self.cashfree)
        with patch.object(gateway, "request") as send, self.assertRaises(GatewayRejected):
            gateway.create_order(attempt)
        send.assert_not_called()

    def test_duplicate_order_response_is_uncertain_not_rejected(self):
        gateway = CashfreeTest(self.cashfree)
        error = urllib.error.HTTPError("https://sandbox.cashfree.com/pg/orders", 409, "Conflict", {}, io.BytesIO(b"{}"))
        with patch("checkout.gateways.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(GatewayError) as caught:
                gateway.request("POST", "orders", {})
        self.assertNotIsInstance(caught.exception, GatewayRejected)

    def test_payment_statuses_and_exact_amounts(self):
        gateway = CashfreeTest(self.cashfree)
        base = {"cf_payment_id": 42, "order_id": "order_x", "payment_amount": 1180.5, "payment_currency": "INR"}
        self.assertEqual(gateway.payment({**base, "payment_status": "SUCCESS"})["status"], "captured")
        self.assertEqual(gateway.payment({**base, "payment_status": "USER_DROPPED"})["status"], "failed")
        self.assertEqual(gateway.payment({**base, "payment_status": "NEW_THING"})["status"], "unknown")
        self.assertEqual(gateway.payment({**base, "payment_status": "SUCCESS"})["amount"], 118050)
        for bad in [1180.505, True, "abc", -1]:
            with self.assertRaises(GatewayError):
                paise_from_rupees(bad)

    def test_webhook_signature_and_event(self):
        gateway = CashfreeTest(self.cashfree)
        body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
        good = base64.b64encode(hmac.new(CF_SECRET.encode(), b"123" + body, hashlib.sha256).digest()).decode()
        self.assertTrue(gateway.valid_webhook(body, f"123:{good}"))
        self.assertFalse(gateway.valid_webhook(body, f"124:{good}"))
        self.assertFalse(gateway.valid_webhook(body, ":"))
        event = {"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {"order": {"order_id": "order_a"}, "payment": {"cf_payment_id": 9}}}
        self.assertEqual(gateway.webhook_event(event), ("PAYMENT_SUCCESS_WEBHOOK", "pay_9", "order_a"))
        self.assertEqual(gateway.webhook_event({"type": "REFUND_STATUS_WEBHOOK"}), ("REFUND_STATUS_WEBHOOK", None, None))

    def test_only_sandbox_app_ids_are_accepted(self):
        self.assertFalse(CashfreeForm({"key_id": "PROD1234abcd", "key_secret": "secret-123"}).is_valid())
        self.assertTrue(CashfreeForm({"key_id": "TEST1234abcd", "key_secret": "secret-123"}).is_valid())


class ScreenTests(RoutingTestCase):
    def url(self, name, *args):
        return reverse(name, args=[self.workspace.pk, *args])

    def test_routing_screens_are_admin_only(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url("checkout_routing")).status_code, 403)
        self.assertEqual(self.client.post(self.url("checkout_routing_policy"), {"mode": "automatic"}).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url("checkout_routing")).status_code, 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url("checkout_gateway_provider", "stripe")).status_code, 404)

    def test_policy_requires_a_default_with_two_gateways(self):
        form = PolicyForm({"mode": "default", "default_provider": "", "priority": "fees"}, connected={"razorpay", "cashfree"})
        self.assertIn("default_provider", form.errors)
        self.client.force_login(self.owner)
        self.client.post(self.url("checkout_routing_policy"), {"mode": "automatic", "default_provider": "cashfree", "priority": "settlement"})
        policy = RoutingPolicy.objects.get(workspace=self.workspace)
        self.assertEqual((policy.mode, policy.default_provider, policy.priority), ("automatic", "cashfree", "settlement"))

    def test_profile_fees_are_both_or_neither(self):
        self.assertFalse(ProfileForm({"fee_percent": "2", "recurring": "unknown"}).is_valid())
        self.assertTrue(ProfileForm({"fee_percent": "2", "fee_fixed": "0", "recurring": "unknown"}).is_valid())
        self.assertTrue(ProfileForm({"recurring": "unknown"}).is_valid())

    def test_override_is_scoped_and_logged(self):
        foreign_customer = Customer.objects.create(workspace=self.foreign, name="F")
        foreign = Invoice.objects.create(workspace=self.foreign, customer=foreign_customer, created_by=self.outsider, is_sandbox=True)
        self.client.force_login(self.owner)
        response = self.client.post(self.url("checkout_routing_override"), {"invoice": foreign.pk, "provider": "cashfree", "reason": "x"})
        self.assertEqual(response.status_code, 400)
        invoice = self.invoice()
        self.client.post(self.url("checkout_routing_override"), {"invoice": invoice.pk, "provider": "cashfree", "reason": "Customer request"})
        route = InvoiceRoute.objects.get(invoice=invoice)
        page = self.client.get(self.url("checkout_routing"))
        self.assertContains(page, "Customer request")
        self.client.post(self.url("checkout_routing_override_remove", route.pk))
        self.assertFalse(InvoiceRoute.objects.exists())

    def test_recommendation_uses_only_entered_details(self):
        self.profile("razorpay", methods=["upi", "card"], fee_percent=D("2"), fee_fixed=D("0"))
        self.profile("cashfree", methods=["upi"], fee_percent=D("1"), fee_fixed=D("0"))
        rows = routing.recommend(self.workspace, {"methods": ["card"], "recurring": False, "amount": D("1000"), "priority": "fees"})
        self.assertEqual([r["provider"] for r in rows], ["razorpay", "cashfree"])
        self.assertIn("Not enabled for Cards", rows[1]["misses"])
        rows = routing.recommend(self.workspace, {"methods": ["upi"], "recurring": True, "amount": None, "priority": "fees"})
        self.assertTrue(all("Recurring support not confirmed" in r["gaps"] for r in rows))
        self.client.force_login(self.owner)
        page = self.client.get(self.url("checkout_routing") + "?methods=card&amount=1000&priority=fees")
        self.assertContains(page, "Recommended")
