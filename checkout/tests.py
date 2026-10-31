import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.core import signing
from django.db import (
    IntegrityError,
    transaction,
    connection,
    close_old_connections,
    connections,
)
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import services as billing
from billing.forms import PaymentForm
from billing.models import (
    Customer,
    Discount,
    Invoice,
    InvoiceLine,
    Payment,
    Plan,
    Product,
    Tax,
)
from core.models import Membership, User, Workspace
from . import services
from .crypto import CheckoutError, decrypt
from .forms import GatewayForm, LinkForm
from .gateways import GatewayError, GatewayRejected, RazorpayTest
from .models import (
    CheckoutSession,
    GatewayAccount,
    GatewayEvent,
    PaymentAttempt,
    ShareLink,
)
from unittest import skipUnless

KEY_SECRET = "test-secret-never-real-12345"
WEBHOOK_SECRET = "webhook-secret-never-real-12345"


@override_settings(
    PAYMENT_ENCRYPTION_KEYS=Fernet.generate_key().decode(),
    MANDALI_BASE_URL="http://testserver",
)
class CheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "owner@example.com", "Local-test!Owner42", first_name="Owner"
        )
        cls.staff = User.objects.create_user("staff@example.com", "Local-test!Staff42")
        cls.other = User.objects.create_user("other@example.com", "Local-test!Other42")
        cls.workspace = Workspace.objects.create(
            name="Example Studio",
            legal_name="Example Studio LLP",
            billing_email="accounts@example.com",
            address="Example address",
        )
        cls.foreign = Workspace.objects.create(
            name="Other Studio",
            legal_name="Other Studio LLP",
            billing_email="other@example.com",
            address="Other address",
        )
        Membership.objects.create(workspace=cls.workspace, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.workspace, user=cls.staff, role="staff")
        Membership.objects.create(workspace=cls.foreign, user=cls.other, role="admin")
        cls.tax = Tax.objects.create(workspace=cls.workspace, name="GST", rate=18)
        cls.product = Product.objects.create(
            workspace=cls.workspace,
            name="Example service",
            sales_price=1000,
            tax=cls.tax,
        )
        cls.plan = Plan.objects.create(
            workspace=cls.workspace, name="Monthly plan", price=500, min_quantity=2
        )
        cls.customer = Customer.objects.create(
            workspace=cls.workspace,
            name="Private customer",
            email="buyer@example.com",
            billing_address="PRIVATE STORED ADDRESS",
        )
        cls.account = services.configure_gateway(
            cls.workspace,
            cls.owner,
            {
                "key_id": "rzp_test_example123",
                "key_secret": KEY_SECRET,
                "webhook_secret": WEBHOOK_SECRET,
            },
        )

    def invoice(self, sandbox=True):
        invoice = Invoice.objects.create(
            workspace=self.workspace,
            customer=self.customer,
            created_by=self.owner,
            is_sandbox=sandbox,
        )
        billing.add_invoice_line(invoice, {"product": self.product}, 1)
        billing.issue_invoice(invoice, self.owner)
        return invoice

    def link(self, invoice=None, product=None, plan=None, quantity=1):
        return services.create_link(
            self.workspace,
            self.owner,
            {
                "title": "Example payment",
                "invoice": invoice,
                "product": product,
                "plan": plan,
                "quantity": quantity,
                "valid_days": 7,
            },
        )

    def buyer(self):
        return {
            "name": "Submitted buyer",
            "email": "buyer@example.com",
            "phone": "9999999999",
            "address": "SUBMITTED ADDRESS",
        }

    def test_key_rotation_retains_old_ciphertexts_and_uses_new_key(self):
        from django.conf import settings
        from .crypto import encrypt

        old_key = settings.PAYMENT_ENCRYPTION_KEYS
        old_value = encrypt("old value")
        new_key = Fernet.generate_key().decode()
        with override_settings(PAYMENT_ENCRYPTION_KEYS=new_key + "," + old_key):
            self.assertEqual(decrypt(old_value), "old value")
            self.assertEqual(
                Fernet(new_key.encode()).decrypt(encrypt("new").encode()), b"new"
            )
        with override_settings(PAYMENT_ENCRYPTION_KEYS=new_key):
            with self.assertRaises(CheckoutError):
                decrypt(old_value)

    def test_provider_transport_rejects_malformed_history_without_releasing_order(self):
        gateway = RazorpayTest(self.account)
        with patch.object(gateway, "request", return_value={"items": [None]}):
            with self.assertRaises(GatewayError):
                gateway.order_payments("order_example123")

    def test_sandbox_events_do_not_trigger_real_website_fulfillment(self):
        with patch("integrations.webhooks.queue") as queue:
            session = self.session()
            attempt = self.start(session)
            services.apply_payment(attempt, self.evidence(attempt))
        queue.assert_not_called()

    def test_order_post_timeout_and_rate_limit_are_uncertain_not_rejected(self):
        import urllib.error

        gateway = RazorpayTest(self.account)
        for error in [
            TimeoutError(),
            urllib.error.HTTPError(
                "https://api.razorpay.com", 429, "limited", {}, None
            ),
        ]:
            with patch("checkout.gateways.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(GatewayError) as raised:
                    gateway.request("POST", "orders", {})
                self.assertNotIsInstance(raised.exception, GatewayRejected)

    def session(self, invoice=None):
        link = (
            self.link(invoice=invoice) if invoice else self.link(product=self.product)
        )
        current = services.quote(link)
        return services.create_session(
            link, self.buyer(), services.nonce(link, current)
        )

    def order(self, attempt):
        return {
            "id": "order_" + attempt.pk.hex,
            "amount": attempt.amount_paise,
            "currency": "INR",
            "receipt": attempt.pk.hex,
            "notes": {"mandali_attempt": str(attempt.pk)},
        }

    def start(self, session):
        with patch.object(
            RazorpayTest,
            "create_order",
            autospec=True,
            side_effect=lambda gateway, attempt: self.order(attempt),
        ) as create:
            result = services.begin(session)
        session.refresh_from_db()
        return result

    def evidence(
        self, attempt, payment_id="pay_example123", status="captured", **kwargs
    ):
        return {
            "id": payment_id,
            "order_id": attempt.provider_order_id,
            "amount": attempt.amount_paise,
            "currency": "INR",
            "status": status,
            "captured": status == "captured",
            "amount_refunded": 0,
            **kwargs,
        }

    def callback_signature(self, attempt, payment_id="pay_example123"):
        return hmac.new(
            KEY_SECRET.encode(),
            f"{attempt.provider_order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def event(
        self, attempt, payment_id="pay_example123", event_type="payment.captured"
    ):
        body = json.dumps(
            {
                "event": event_type,
                "payload": {"payment": {"entity": self.evidence(attempt, payment_id)}},
            }
        ).encode()
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return body, signature

    def admin_url(self, name, **kwargs):
        return reverse(name, kwargs={"workspace_id": self.workspace.pk, **kwargs})

    def test_credentials_and_link_tokens_are_encrypted(self):
        self.assertNotIn(KEY_SECRET, self.account.secrets_ciphertext)
        self.assertNotIn(WEBHOOK_SECRET, self.account.secrets_ciphertext)
        self.assertIn(KEY_SECRET, decrypt(self.account.secrets_ciphertext))
        link = self.link(product=self.product)
        token = decrypt(link.token_ciphertext)
        self.assertNotEqual(token, link.token_digest)
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), link.token_digest)
        self.assertEqual(
            self.client.get(urlsplit(services.link_url(link)).path).status_code, 200
        )

    def test_live_keys_are_rejected(self):
        form = GatewayForm(
            {
                "key_id": "rzp_live_not_allowed",
                "key_secret": KEY_SECRET,
                "webhook_secret": WEBHOOK_SECRET,
            }
        )
        self.assertFalse(form.is_valid())
        self.account.key_id = "rzp_live_unsafe"
        with self.assertRaises(CheckoutError):
            RazorpayTest(self.account)

    def test_staff_and_other_workspace_cannot_configure_gateway(self):
        for user, status in [(self.staff, 403), (self.other, 404)]:
            self.client.force_login(user)
            self.assertEqual(
                self.client.post(
                    self.admin_url("checkout_gateway"),
                    {
                        "key_id": "rzp_test_abc",
                        "key_secret": KEY_SECRET,
                        "webhook_secret": WEBHOOK_SECRET,
                    },
                ).status_code,
                status,
            )
        self.assertEqual(GatewayAccount.objects.count(), 1)

    def test_links_and_reconciliation_are_workspace_scoped(self):
        link = self.link(product=self.product)
        session = self.session()
        attempt = self.start(session)
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(
                self.admin_url("checkout_link_detail", pk=link.pk)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                self.admin_url("checkout_link_revoke", pk=link.pk)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                self.admin_url("checkout_attempt_reconcile", pk=attempt.pk)
            ).status_code,
            404,
        )

    def test_link_form_and_service_reject_cross_workspace_targets(self):
        product = Product.objects.create(
            workspace=self.foreign, name="Private item", sales_price=100
        )
        data = {
            "title": "Foreign",
            "product": product.pk,
            "quantity": 1,
            "valid_days": 7,
        }
        self.assertFalse(LinkForm(data, workspace=self.workspace).is_valid())
        with self.assertRaises(CheckoutError):
            self.link(product=product)

    def test_real_invoices_cannot_be_used_for_sandbox_checkout(self):
        invoice = self.invoice(sandbox=False)
        self.assertFalse(
            LinkForm(
                {
                    "title": "Wrong",
                    "invoice": invoice.pk,
                    "quantity": 1,
                    "valid_days": 7,
                },
                workspace=self.workspace,
            ).is_valid()
        )
        with self.assertRaises(CheckoutError):
            self.link(invoice=invoice)
        with self.assertRaises(billing.BillingError):
            billing.record_payment(
                invoice,
                None,
                Decimal("1"),
                Payment.Method.RAZORPAY_TEST,
                timezone.localdate(),
            )

    def test_expired_and_revoked_links_reject_new_checkout(self):
        for revoked in [False, True]:
            link = self.link(product=self.product)
            token = decrypt(link.token_ciphertext)
            if revoked:
                ShareLink.objects.filter(pk=link.pk).update(revoked_at=timezone.now())
            else:
                ShareLink.objects.filter(pk=link.pk).update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )
            self.assertEqual(
                self.client.get(reverse("checkout_public", args=[token])).status_code,
                410,
            )

    def test_qr_download_uses_exact_scoped_url(self):
        import segno

        link = self.link(product=self.product)
        self.client.force_login(self.owner)
        with patch("checkout.views.segno.make", wraps=segno.make) as generate:
            response = self.client.get(self.admin_url("checkout_link_qr", pk=link.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", response.content)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(generate.call_args.args[0], services.link_url(link))

    def test_idempotent_public_form_and_private_customer_snapshot(self):
        link = self.link(product=self.product)
        nonce = services.nonce(link, services.quote(link))
        first = services.create_session(link, self.buyer(), nonce)
        second = services.create_session(link, self.buyer(), nonce)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Invoice.objects.count(), 1)
        invoice = Invoice.objects.get()
        self.customer.refresh_from_db()
        self.assertTrue(invoice.is_sandbox)
        self.assertEqual(invoice.source_channel, "hosted")
        self.assertTrue(invoice.number.startswith("TST-"))
        self.assertEqual(invoice.customer_address, "SUBMITTED ADDRESS")
        self.assertEqual(self.customer.billing_address, "PRIVATE STORED ADDRESS")
        response = self.client.get(services.session_url(first))
        self.assertContains(response, "SUBMITTED ADDRESS")
        self.assertNotContains(response, "PRIVATE STORED ADDRESS")

    def test_reusable_link_creates_separate_sessions_and_invoices(self):
        link = self.link(product=self.product)
        for _ in range(2):
            services.create_session(
                link, self.buyer(), services.nonce(link, services.quote(link))
            )
        self.assertEqual(CheckoutSession.objects.count(), 2)
        self.assertEqual(Invoice.objects.count(), 2)

    def test_changed_catalog_price_requires_review_before_invoice_creation(self):
        link = self.link(product=self.product)
        nonce = services.nonce(link, services.quote(link))
        Product.objects.filter(pk=self.product.pk).update(sales_price=2000)
        with self.assertRaisesMessage(CheckoutError, "price or balance changed"):
            services.create_session(link, self.buyer(), nonce)
        self.assertFalse(Invoice.objects.exists())

    def test_nonexistent_or_foreign_nonce_and_tampered_session_fail(self):
        link = self.link(product=self.product)
        other = self.link(product=self.product)
        with self.assertRaises(CheckoutError):
            services.create_session(
                other, self.buyer(), services.nonce(link, services.quote(link))
            )
        with self.assertRaises(CheckoutError):
            services.create_session(link, self.buyer(), "tampered")
        session = self.session()
        token = services.session_token(session)
        self.assertEqual(
            self.client.get(
                reverse("checkout_session", args=[token + "tamper"])
            ).status_code,
            410,
        )
        CheckoutSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(
            self.client.get(reverse("checkout_session", args=[token])).status_code, 410
        )

    def test_csrf_protects_public_create_begin_and_callback(self):
        client = Client(enforce_csrf_checks=True)
        session = self.session()
        token = services.session_token(session)
        self.assertEqual(
            client.post(reverse("checkout_begin", args=[token])).status_code, 403
        )
        self.assertEqual(
            client.post(reverse("checkout_verify", args=[token])).status_code, 403
        )
        link = self.link(product=self.product)
        self.assertEqual(
            client.post(urlsplit(services.link_url(link)).path, {}).status_code, 403
        )

    def test_server_amount_and_existing_order_are_reused(self):
        session = self.session()
        first = self.start(session)
        with patch.object(RazorpayTest, "create_order") as create:
            second = services.begin(session)
        self.assertEqual(first.pk, second.pk)
        create.assert_not_called()
        self.assertEqual(first.amount_paise, 118000)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 0)

    def test_order_creation_timeout_blocks_automatic_duplicate_creation(self):
        session = self.session()
        with patch.object(
            RazorpayTest,
            "create_order",
            side_effect=GatewayError("Uncertain network response"),
        ):
            attempt = services.begin(session)
        self.assertEqual(attempt.status, "unknown")
        with patch.object(RazorpayTest, "create_order") as create:
            self.assertEqual(services.begin(session).pk, attempt.pk)
        create.assert_not_called()
        with patch.object(
            RazorpayTest, "fetch_order", return_value=self.order(attempt)
        ), patch.object(RazorpayTest, "order_payments", return_value=[]):
            result = services.reconcile(attempt, self.order(attempt)["id"])
        self.assertEqual(result.status, "ready")
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_rejected_order_can_be_prepared_again_without_claiming_payment(self):
        session = self.session()
        with patch.object(
            RazorpayTest,
            "create_order",
            side_effect=GatewayRejected("Configuration rejected"),
        ):
            attempt = services.begin(session)
        self.assertEqual(attempt.status, "rejected")
        second = self.start(session)
        self.assertNotEqual(attempt.pk, second.pk)
        self.assertFalse(Payment.objects.exists())

    def test_malformed_order_response_is_not_offered_to_checkout(self):
        session = self.session()
        with patch.object(
            RazorpayTest,
            "create_order",
            return_value={
                "id": "order_fake",
                "amount": 1,
                "currency": "INR",
                "receipt": "wrong",
                "notes": {},
            },
        ):
            attempt = services.begin(session)
        self.assertEqual(attempt.status, "unknown")
        self.assertEqual(attempt.provider_order_id, "")

    def test_invalid_callback_signature_does_not_fetch_or_record(self):
        session = self.session()
        attempt = self.start(session)
        with patch.object(RazorpayTest, "fetch_payment") as fetch:
            with self.assertRaises(GatewayError):
                services.verify_callback(
                    session, "pay_example123", attempt.provider_order_id, "0" * 64
                )
        fetch.assert_not_called()
        self.assertFalse(Payment.objects.exists())

    def test_authorization_alone_does_not_settle_invoice(self):
        session = self.session()
        attempt = self.start(session)
        result = services.apply_payment(
            attempt, self.evidence(attempt, status="authorized")
        )
        session.invoice.refresh_from_db()
        self.assertEqual(result.status, "authorized")
        self.assertEqual(session.invoice.amount_paid, 0)
        self.assertFalse(Payment.objects.exists())

    def test_verified_capture_settles_once_and_receipt_renders(self):
        session = self.session()
        attempt = self.start(session)
        with patch.object(
            RazorpayTest, "fetch_payment", return_value=self.evidence(attempt)
        ):
            for _ in range(2):
                services.verify_callback(
                    session,
                    "pay_example123",
                    attempt.provider_order_id,
                    self.callback_signature(attempt),
                )
        session.invoice.refresh_from_db()
        self.assertEqual(session.invoice.status, Invoice.Status.PAID)
        self.assertEqual(session.invoice.amount_paid, Decimal("1180"))
        self.assertEqual(Payment.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertIsNone(payment.recorded_by_id)
        self.assertEqual(payment.method, Payment.Method.RAZORPAY_TEST)
        response = self.client.get(services.session_url(session))
        self.assertContains(response, "Sandbox receipt")
        self.assertContains(response, "pay_example123")

    def test_wrong_order_amount_currency_or_payment_id_cannot_settle(self):
        for override in [
            {"order_id": "order_other"},
            {"amount": 1},
            {"amount": True},
            {"currency": "USD"},
        ]:
            session = self.session()
            attempt = self.start(session)
            with self.assertRaises(GatewayError):
                services.apply_payment(attempt, self.evidence(attempt, **override))
        self.assertFalse(Payment.objects.exists())
        session = self.session()
        attempt = self.start(session)
        with patch.object(
            RazorpayTest,
            "fetch_payment",
            return_value=self.evidence(attempt, payment_id="pay_other"),
        ):
            with self.assertRaises(GatewayError):
                services.verify_callback(
                    session,
                    "pay_example123",
                    attempt.provider_order_id,
                    self.callback_signature(attempt),
                )

    def test_refund_or_overpayment_is_reviewed_without_rewriting_ledger(self):
        session = self.session()
        attempt = self.start(session)
        result = services.apply_payment(
            attempt, self.evidence(attempt, amount_refunded=100)
        )
        self.assertEqual(result.status, "review")
        self.assertFalse(Payment.objects.exists())
        other = self.session()
        pending = self.start(other)
        billing.record_payment(
            other.invoice,
            self.owner,
            Decimal("100"),
            Payment.Method.CASH,
            timezone.localdate(),
        )
        result = services.apply_payment(
            pending, self.evidence(pending, payment_id="pay_conflict")
        )
        self.assertEqual(result.status, "review")
        other.invoice.refresh_from_db()
        self.assertEqual(other.invoice.amount_paid, Decimal("100"))
        self.assertEqual(Payment.objects.count(), 1)

    def test_manual_reference_conflict_is_not_automatically_recorded_again(self):
        prior = self.invoice()
        billing.record_payment(
            prior,
            self.owner,
            Decimal("1"),
            Payment.Method.CASH,
            timezone.localdate(),
            reference="pay_example123",
        )
        session = self.session()
        attempt = self.start(session)
        result = services.apply_payment(attempt, self.evidence(attempt))
        self.assertEqual(result.status, "review")
        self.assertEqual(Payment.objects.count(), 1)

    def test_provider_reference_uniqueness_is_transactional(self):
        first = self.session()
        attempt = self.start(first)
        services.apply_payment(attempt, self.evidence(attempt))
        second = self.session()
        another = self.start(second)
        # The reference guard detects existing ledger entries even before the
        # database uniqueness backstop would be needed.
        result = services.apply_payment(another, self.evidence(another))
        self.assertEqual(result.status, "review")
        self.assertEqual(Payment.objects.count(), 1)
        second.invoice.refresh_from_db()
        self.assertEqual(second.invoice.amount_paid, 0)

    def test_invalid_webhook_signature_is_rejected_before_network(self):
        session = self.session()
        attempt = self.start(session)
        body, _ = self.event(attempt)
        with patch.object(RazorpayTest, "fetch_payment") as fetch:
            result = self.client.post(
                reverse("checkout_webhook", args=[self.account.pk]),
                data=body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="bad",
                HTTP_X_RAZORPAY_EVENT_ID="event1",
            )
        self.assertEqual(result.status_code, 400)
        fetch.assert_not_called()
        self.assertFalse(GatewayEvent.objects.exists())

    def test_webhook_capture_replay_and_callback_are_idempotent(self):
        session = self.session()
        attempt = self.start(session)
        body, signature = self.event(attempt)
        with patch.object(
            RazorpayTest, "fetch_payment", return_value=self.evidence(attempt)
        ) as fetch:
            self.assertEqual(
                services.process_webhook(self.account, body, signature, "event1"),
                "paid",
            )
            self.assertEqual(
                services.process_webhook(self.account, body, signature, "event1"),
                "duplicate",
            )
            self.assertEqual(
                services.process_webhook(
                    self.account, body, signature, "changed-header"
                ),
                "duplicate",
            )
            services.verify_callback(
                session,
                "pay_example123",
                attempt.provider_order_id,
                self.callback_signature(attempt),
            )
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(GatewayEvent.objects.count(), 1)
        self.assertEqual(fetch.call_count, 2)

    def test_webhook_transient_failure_retries_without_storing_processed_event(self):
        session = self.session()
        attempt = self.start(session)
        body, signature = self.event(attempt)
        with patch.object(
            RazorpayTest,
            "fetch_payment",
            side_effect=GatewayError("Network unavailable"),
        ):
            response = self.client.post(
                reverse("checkout_webhook", args=[self.account.pk]),
                data=body,
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE=signature,
                HTTP_X_RAZORPAY_EVENT_ID="event1",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(GatewayEvent.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_webhook_recovers_order_creation_timeout_using_provider_notes(self):
        session = self.session()
        with patch.object(
            RazorpayTest, "create_order", side_effect=GatewayError("Unknown")
        ):
            attempt = services.begin(session)
        order = self.order(attempt)
        attempt.provider_order_id = order["id"]
        body, signature = self.event(attempt)
        attempt.provider_order_id = ""
        with patch.object(
            RazorpayTest, "fetch_order", return_value=order
        ), patch.object(
            RazorpayTest,
            "fetch_payment",
            return_value={**self.evidence(attempt), "order_id": order["id"]},
        ):
            result = services.process_webhook(
                self.account, body, signature, "event-recovered"
            )
        self.assertEqual(result, "paid")
        self.assertEqual(Payment.objects.count(), 1)

    def test_late_authorized_or_failed_event_does_not_unpay_invoice(self):
        session = self.session()
        attempt = self.start(session)
        services.apply_payment(attempt, self.evidence(attempt))
        for status in ["authorized", "failed"]:
            services.apply_payment(attempt, self.evidence(attempt, status=status))
        session.invoice.refresh_from_db()
        self.assertEqual(session.invoice.status, "paid")
        self.assertEqual(Payment.objects.count(), 1)

    def test_revocation_blocks_start_but_retains_legitimate_capture_evidence(self):
        session = self.session()
        attempt = self.start(session)
        ShareLink.objects.filter(pk=session.link_id).update(revoked_at=timezone.now())
        with self.assertRaises(CheckoutError):
            services.begin(session)
        services.apply_payment(attempt, self.evidence(attempt))
        self.assertEqual(Payment.objects.count(), 1)

    def test_gateway_rotation_preserves_pending_evidence(self):
        session = self.session()
        attempt = self.start(session)
        new = services.configure_gateway(
            self.workspace,
            self.owner,
            {
                "key_id": "rzp_test_different456",
                "key_secret": "new-secret-12345",
                "webhook_secret": "new-webhook-secret-12345",
            },
        )
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_active)
        self.assertNotEqual(new.pk, attempt.gateway_id)
        with patch.object(
            RazorpayTest, "fetch_payment", return_value=self.evidence(attempt)
        ):
            services.verify_callback(
                session,
                "pay_example123",
                attempt.provider_order_id,
                self.callback_signature(attempt),
            )
        self.assertEqual(Payment.objects.count(), 1)

    def test_disabled_gateway_and_missing_encryption_fail_safely(self):
        session = self.session()
        GatewayAccount.objects.filter(pk=self.account.pk).update(is_active=False)
        with self.assertRaises(CheckoutError):
            services.begin(session)
        self.assertFalse(PaymentAttempt.objects.exists())
        with override_settings(PAYMENT_ENCRYPTION_KEYS=""):
            with self.assertRaises(CheckoutError):
                self.link(product=self.product)

    def test_sandbox_totals_and_discount_uses_are_not_real_business_totals(self):
        discount = Discount.objects.create(
            workspace=self.workspace,
            name="Test discount",
            value=10,
            kind="percent",
            usage_limit=3,
            times_used=1,
        )
        invoice = Invoice.objects.create(
            workspace=self.workspace,
            customer=self.customer,
            created_by=self.owner,
            is_sandbox=True,
            discount=discount,
        )
        billing.add_invoice_line(invoice, {"product": self.product}, 1)
        billing.issue_invoice(invoice, self.owner)
        self.assertEqual(
            billing.billing_summary(self.workspace, timezone.localdate())[
                "outstanding"
            ],
            Decimal("0"),
        )
        discount.refresh_from_db()
        self.assertEqual(discount.times_used, 1)
        billing.cancel_invoice(invoice, self.owner, "Sandbox cancelled")
        discount.refresh_from_db()
        self.assertEqual(discount.times_used, 1)

    def test_gateway_payments_cannot_be_forged_in_manual_form_or_reversed_offline(self):
        form = PaymentForm(
            {
                "amount": "100",
                "method": Payment.Method.RAZORPAY_TEST,
                "received_on": timezone.localdate().isoformat(),
            }
        )
        self.assertFalse(form.is_valid())
        session = self.session()
        attempt = self.start(session)
        services.apply_payment(attempt, self.evidence(attempt))
        with self.assertRaises(billing.BillingError):
            billing.reverse_payment(Payment.objects.get(), self.owner, "Pretend refund")
        session.invoice.refresh_from_db()
        self.assertEqual(session.invoice.status, "paid")

    def test_gateway_script_policy_only_on_ready_public_checkout(self):
        session = self.session()
        attempt = self.start(session)
        response = self.client.get(services.session_url(session))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "https://checkout.razorpay.com", response["Content-Security-Policy"]
        )
        self.assertNotIn(
            "https://checkout.razorpay.com",
            self.client.get(reverse("login"))["Content-Security-Policy"],
        )
        self.assertNotIn(KEY_SECRET, response.content.decode())
        self.assertNotIn(WEBHOOK_SECRET, response.content.decode())
        self.assertContains(response, "Check payment status")

    def test_plan_checkout_is_only_an_initial_invoice(self):
        link = self.link(plan=self.plan, quantity=2)
        session = services.create_session(
            link, self.buyer(), services.nonce(link, services.quote(link))
        )
        self.assertEqual(session.invoice.total, Decimal("1000"))
        self.assertIsNone(session.invoice.subscription_id)
        self.assertContains(
            self.client.get(urlsplit(services.link_url(link)).path),
            "No automatic renewal",
        )


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency")
@override_settings(
    PAYMENT_ENCRYPTION_KEYS=Fernet.generate_key().decode(),
    MANDALI_BASE_URL="http://testserver",
)
class CheckoutConcurrencyTests(TransactionTestCase):
    invoice = CheckoutTests.invoice
    link = CheckoutTests.link
    buyer = CheckoutTests.buyer
    session = CheckoutTests.session
    order = CheckoutTests.order
    start = CheckoutTests.start
    evidence = CheckoutTests.evidence

    def setUp(self):
        CheckoutTests.setUpTestData.__func__(type(self))

    def parallel(self, action):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        barrier = Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return action()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(worker) for _ in range(2)]
            return [job.result(timeout=20) for job in jobs]

    def test_concurrent_begin_reserves_one_gateway_order(self):
        session = self.session()
        with patch.object(
            RazorpayTest,
            "create_order",
            autospec=True,
            side_effect=lambda gateway, attempt: self.order(attempt),
        ) as create:
            attempts = self.parallel(
                lambda: services.begin(CheckoutSession.objects.get(pk=session.pk))
            )
        self.assertEqual(create.call_count, 1)
        self.assertEqual(attempts[0].pk, attempts[1].pk)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_concurrent_capture_records_one_ledger_payment(self):
        session = self.session()
        attempt = self.start(session)
        results = self.parallel(
            lambda: services.apply_payment(
                PaymentAttempt.objects.get(pk=attempt.pk), self.evidence(attempt)
            )
        )
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(results[0].payment_id, results[1].payment_id)
        session.invoice.refresh_from_db()
        self.assertEqual(session.invoice.amount_paid, session.invoice.total)
        self.assertEqual(session.invoice.status, "paid")
