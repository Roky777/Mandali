import hashlib
import hmac
import json
import socket
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from billing import services
from billing.models import Customer, Invoice, Plan, Product, Subscription, Tax
from core.models import Membership, User, Workspace
from . import webhooks
from .keys import create_key, digest
from .models import ApiKey, Connector, IdempotencyRecord, SyncRun, WebhookDelivery, WebhookEndpoint
from .outbound import OutboundError, check_url, public_address
from .sync import apply_run, fetch_preview

PASSWORD = "Local-test!42Safe"
D = Decimal


class IntegrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner@example.com", PASSWORD, first_name="Owner")
        cls.staff = User.objects.create_user("staff@example.com", PASSWORD, first_name="Staff")
        cls.outsider = User.objects.create_user("other@example.com", PASSWORD)
        cls.business = Workspace.objects.create(
            name="Example Studio", legal_name="Example Studio LLP",
            billing_email="accounts@example.com", address="Example address", use_api=True,
        )
        cls.other = Workspace.objects.create(
            name="Other Business", legal_name="Other LLP",
            billing_email="other@example.com", address="Another address", use_api=True,
        )
        Membership.objects.create(workspace=cls.business, user=cls.owner, role="admin")
        Membership.objects.create(workspace=cls.business, user=cls.staff, role="staff")
        Membership.objects.create(workspace=cls.other, user=cls.outsider, role="admin")
        gst = Tax.objects.create(workspace=cls.business, name="GST 18%", rate=D("18"))
        cls.product = Product.objects.create(
            workspace=cls.business, name="Design retainer", code="RETAINER", sales_price=D("1000"), tax=gst
        )
        cls.plan = Plan.objects.create(
            workspace=cls.business, name="Studio", code="STUDIO", price=D("500"), min_quantity=2
        )
        Product.objects.create(workspace=cls.other, name="Private", code="PRIVATE", sales_price=D("5"))

    def setUp(self):
        self.key, self.raw = create_key(self.business, self.owner, "Shop website")
        self.api = Client(enforce_csrf_checks=True)

    def post(self, path, body, raw=None, **headers):
        return self.api.post(
            f"/api/v1/{path}",
            json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}",
            **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()},
        )

    def get(self, path, raw=None):
        return self.api.get(f"/api/v1/{path}", HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def order(self, external_id="order-1", **extra):
        return {
            "external_id": external_id,
            "customer": {"external_id": "cust-1", "name": "Asha Traders", "email": "asha@example.com"},
            "lines": [{"product": "RETAINER", "quantity": 2}],
            **extra,
        }


class ApiTests(IntegrationTestCase):
    def test_keys_are_hashed_and_required(self):
        self.assertTrue(self.raw.startswith("mdl_"))
        stored = ApiKey.objects.get(pk=self.key.pk)
        self.assertEqual(stored.digest, digest(self.raw))
        self.assertNotIn(self.raw, json.dumps(list(ApiKey.objects.values()), default=str))
        self.assertEqual(self.api.get("/api/v1/ping").status_code, 401)
        self.assertEqual(self.get("ping", raw="mdl_wrong").status_code, 401)
        self.assertEqual(self.get("ping").json()["workspace"], "Example Studio")
        ApiKey.objects.filter(pk=self.key.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.get("ping").status_code, 401)

    def test_session_login_is_not_api_authentication(self):
        self.api.force_login(self.owner)
        response = self.api.post("/api/v1/orders", json.dumps(self.order()), content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_channel_switch_controls_api_access(self):
        Workspace.objects.filter(pk=self.business.pk).update(use_api=False)
        self.assertEqual(self.get("ping").json()["error"]["code"], "channel_disabled")

    def test_order_creates_issued_invoice_with_server_totals(self):
        response = self.post("orders", self.order(total="1.00", status="paid"))
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["status"], body["number"], body["total"]), ("issued", "INV-00001", "2360.00"))
        invoice = Invoice.objects.get(external_id="order-1")
        self.assertEqual((invoice.source_channel, invoice.customer.source), ("api", "api"))
        self.assertEqual(invoice.customer.external_id, "cust-1")

    def test_replayed_order_is_not_duplicated(self):
        first = self.post("orders", self.order())
        second = self.post("orders", self.order())
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(first.json(), second.json())
        self.assertEqual(Invoice.objects.count(), 1)
        changed = self.post("orders", self.order(notes="Different"))
        self.assertEqual(changed.json()["error"]["code"], "idempotency_mismatch")
        other_key = self.post("orders", self.order(), **{"Idempotency-Key": "fresh-key"})
        self.assertEqual(other_key.json()["error"]["code"], "duplicate_order")
        self.assertEqual(Invoice.objects.count(), 1)

    def test_rejected_request_rolls_back_and_can_be_corrected(self):
        bad = self.order(lines=[{"product": "MISSING"}])
        response = self.post("orders", bad, **{"Idempotency-Key": "k1"})
        self.assertEqual(response.json()["error"]["code"], "unknown_item")
        self.assertFalse(Customer.objects.filter(external_id="cust-1").exists())
        self.assertFalse(IdempotencyRecord.objects.exists())
        self.assertEqual(self.post("orders", self.order(), **{"Idempotency-Key": "k1"}).status_code, 201)

    def test_order_validation(self):
        cases = [
            (self.order(lines=[]), "invalid_lines"),
            (self.order(lines=[{"plan": "STUDIO", "quantity": 1}]), "invalid_lines"),
            (self.order(lines=[{"product": "PRIVATE"}]), "unknown_item"),
            (self.order(customer="nobody"), "unknown_customer"),
            (self.order(customer={"external_id": "c2", "name": "X", "gstin": "bad"}), "invalid_customer"),
            (self.order(payment_terms_days="soon"), "invalid_request"),
            ({"lines": []}, "invalid_record"),
        ]
        for body, code in cases:
            self.assertEqual(self.post("orders", body).json()["error"]["code"], code, body)
        self.assertEqual(Invoice.objects.count(), 0)
        response = self.api.post("/api/v1/orders", "{", content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(response.status_code, 400)
        response = self.api.post("/api/v1/orders", "x", content_type="text/plain", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(response.status_code, 415)

    def test_draft_order_and_status_lookup(self):
        self.post("orders", self.order(issue=False))
        invoice = Invoice.objects.get(external_id="order-1")
        self.assertEqual(invoice.status, "draft")
        services.issue_invoice(invoice, self.owner)
        services.record_payment(invoice, self.owner, D("1000"), "upi", timezone.localdate())
        body = self.get("orders/order-1").json()
        self.assertEqual((body["amount_paid"], body["balance"]), ("1000.00", "1360.00"))
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(self.get("orders/missing").status_code, 404)

    def test_customer_upsert(self):
        created = self.post("customers", {"external_id": "c1", "name": "Ravi", "email": "RAVI@example.com"})
        self.assertEqual((created.status_code, created.json()["result"]), (201, "create"))
        updated = self.post("customers", {"external_id": "c1", "phone": "+91 90000 00000"})
        self.assertEqual(updated.json()["result"], "update")
        customer = Customer.objects.get(external_id="c1")
        self.assertEqual((customer.name, customer.email, customer.phone), ("Ravi", "ravi@example.com", "+91 90000 00000"))
        self.assertEqual(self.post("customers", {"external_id": "c1", "name": "Ravi"}).json()["result"], "unchanged")
        Customer.objects.create(workspace=self.business, name="Manual", email="manual@example.com")
        clash = self.post("customers", {"external_id": "c2", "name": "Copy", "email": "manual@example.com"})
        self.assertEqual(clash.status_code, 422)
        self.assertIn("email", clash.json()["error"]["fields"])
        self.assertEqual(self.get("customers/c1").json()["name"], "Ravi")

    def test_subscription_ingestion(self):
        response = self.post(
            "subscriptions",
            {"external_id": "sub-1", "customer": {"external_id": "c1", "name": "Ravi"}, "plan": "STUDIO", "start_date": "2026-11-01"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        subscription = Subscription.objects.get(external_id="sub-1")
        self.assertEqual((subscription.status, subscription.source_channel), ("draft", "api"))
        self.assertEqual(subscription.lines.get().quantity, 2)
        self.assertEqual(response.json()["total_per_period"], "1000.00")
        self.assertEqual(self.post("subscriptions", {"external_id": "s2", "customer": "c1", "plan": "NOPE"}).status_code, 422)
        self.assertEqual(self.get("subscriptions/sub-1").json()["plan"], "STUDIO")

    def test_keys_cannot_reach_another_workspace(self):
        self.post("orders", self.order())
        _, other_raw = create_key(self.other, self.outsider, "Other")
        self.assertEqual(self.get("orders/order-1", raw=other_raw).status_code, 404)
        self.assertEqual(self.get("customers/cust-1", raw=other_raw).status_code, 404)
        response = self.post("orders", self.order("o-9", customer="cust-1"), raw=other_raw)
        self.assertEqual(response.json()["error"]["code"], "unknown_customer")
        same_id = self.post("orders", self.order(lines=[{"product": "PRIVATE"}]), raw=other_raw)
        self.assertEqual(same_id.status_code, 201)
        self.assertEqual(Invoice.objects.filter(external_id="order-1").count(), 2)

    def test_rate_limit(self):
        with patch("integrations.api.RATE_LIMIT", 2):
            codes = [self.get("ping").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])


def signed(endpoint, body, timestamp):
    return hmac.new(endpoint.secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class WebhookTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.endpoint = WebhookEndpoint.objects.create(
            workspace=self.business, url="https://shop.example.com/hook", secret=webhooks.new_secret(), created_by=self.owner
        )
        self.paused = WebhookEndpoint.objects.create(
            workspace=self.business, url="https://paused.example.com/hook", secret="s", created_by=self.owner, is_active=False
        )
        self.elsewhere = WebhookEndpoint.objects.create(
            workspace=self.other, url="https://other.example.com/hook", secret="s", created_by=self.outsider
        )
        self.customer = Customer.objects.create(workspace=self.business, name="Asha")

    def issued(self):
        invoice = Invoice.objects.create(workspace=self.business, customer=self.customer, created_by=self.owner)
        services.add_invoice_line(invoice, {"product": self.product}, 1)
        services.issue_invoice(invoice, self.owner)
        invoice.refresh_from_db()
        return invoice

    @patch("integrations.webhooks.request", return_value=(200, b""))
    def test_payment_events_are_signed_and_delivered(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            invoice = self.issued()
        with self.captureOnCommitCallbacks(execute=True):
            services.record_payment(invoice, self.staff, invoice.total, "upi", timezone.localdate())
        types = list(WebhookDelivery.objects.order_by("id").values_list("event_type", flat=True))
        self.assertEqual(types, ["invoice.issued", "payment.recorded", "invoice.paid"])
        self.assertFalse(WebhookDelivery.objects.exclude(endpoint=self.endpoint).exists())
        self.assertEqual(WebhookDelivery.objects.filter(status="delivered").count(), 3)
        method, url, headers, body = send.call_args.args
        self.assertEqual((method, url), ("POST", "https://shop.example.com/hook"))
        timestamp, signature = [part.split("=", 1)[1] for part in headers["Mandali-Signature"].split(",")]
        self.assertEqual(signature, signed(self.endpoint, body, timestamp))
        payload = json.loads(body)
        self.assertEqual((payload["type"], payload["data"]["invoice"]["status"]), ("invoice.paid", "paid"))
        self.assertEqual(payload["id"], headers["Mandali-Event-Id"])

    @patch("integrations.webhooks.request", return_value=(500, b""))
    def test_failures_back_off_then_give_up(self, send):
        self.issued()
        delivery = WebhookDelivery.objects.get()
        webhooks.deliver(WebhookDelivery.objects.all())
        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.attempts, delivery.last_status_code), ("pending", 1, 500))
        self.assertGreater(delivery.next_attempt_at, timezone.now() + timedelta(seconds=50))
        self.assertEqual(webhooks.deliver(webhooks.due()), 0)
        self.assertEqual(send.call_count, 1)
        for _ in range(len(webhooks.BACKOFF)):
            WebhookDelivery.objects.update(next_attempt_at=timezone.now())
            webhooks.deliver(webhooks.due())
        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.attempts), ("failed", len(webhooks.BACKOFF) + 1))
        webhooks.resend(delivery)
        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.attempts), ("pending", 0))

    def test_a_claimed_delivery_is_not_sent_twice(self):
        self.issued()
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(webhooks.claim(delivery), 1)
        self.assertEqual(webhooks.claim(delivery), 0)

    @patch("integrations.webhooks.request", side_effect=OutboundError("That address points to a private or internal network."))
    def test_unsafe_endpoint_is_recorded_as_error(self, send):
        self.issued()
        webhooks.deliver(WebhookDelivery.objects.all())
        self.assertIn("private", WebhookDelivery.objects.get().last_error)

    def test_reversal_and_cancel_events(self):
        invoice = self.issued()
        payment = services.record_payment(invoice, self.staff, D("10"), "cash", timezone.localdate())
        services.reverse_payment(payment, self.owner, "Bounced")
        services.cancel_invoice(invoice, self.owner, "Wrong")
        self.assertEqual(
            list(WebhookDelivery.objects.order_by("id").values_list("event_type", flat=True)),
            ["invoice.issued", "payment.recorded", "payment.reversed", "invoice.cancelled"],
        )


def addresses(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


class OutboundTests(TestCase):
    def test_url_rules(self):
        for url in ["http://shop.example.com/", "https://user:pw@shop.example.com/", "https://shop.example.com:8443/", "ftp://x/"]:
            with self.assertRaises(OutboundError, msg=url):
                check_url(url)
        check_url("https://shop.example.com/customers?page=1")

    def test_private_and_metadata_addresses_are_refused(self):
        for ip in ["127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.169.254", "100.64.0.1", "::1", "::ffff:127.0.0.1", "fd00::1"]:
            with patch("socket.getaddrinfo", return_value=addresses(ip)), self.assertRaises(OutboundError, msg=ip):
                public_address("shop.example.com", 443)
        with patch("socket.getaddrinfo", return_value=addresses("93.184.216.34", "10.0.0.1")), self.assertRaises(OutboundError):
            public_address("mixed.example.com", 443)
        with patch("socket.getaddrinfo", return_value=addresses("93.184.216.34")):
            self.assertEqual(public_address("shop.example.com", 443), "93.184.216.34")

    @override_settings(DEBUG=True, MANDALI_ALLOW_LOCAL_INTEGRATIONS=True)
    def test_local_targets_only_with_development_flag(self):
        check_url("http://localhost:8001/customers.json")
        with patch("socket.getaddrinfo", return_value=addresses("127.0.0.1")):
            self.assertEqual(public_address("localhost", 8001), "127.0.0.1")


class SyncTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.connector = Connector.objects.create(
            workspace=self.business, name="Shop", url="https://shop.example.com/api/customers",
            list_path="data.customers",
            mapping={"external_id": "id", "name": "full_name", "email": "contact.email"},
            created_by=self.owner,
        )
        Customer.objects.create(workspace=self.business, name="Old name", external_id="2", source="pull")
        Customer.objects.create(workspace=self.business, name="Manual", email="taken@example.com")
        self.payload = {"data": {"customers": [
            {"id": 1, "full_name": "New Customer", "contact": {"email": "new@example.com"}},
            {"id": 2, "full_name": "Renamed"},
            {"id": 3, "full_name": "Clash", "contact": {"email": "taken@example.com"}},
            {"id": 4, "full_name": "Twin", "contact": {"email": "new@example.com"}},
            {"full_name": "No ID"},
        ]}}

    def fetch(self, status=200, body=None):
        with patch("integrations.sync.request", return_value=(status, json.dumps(body or self.payload).encode())) as send:
            run = fetch_preview(self.connector, self.owner, token="secret-token")
        return run, send

    def test_preview_changes_nothing_until_applied(self):
        run, send = self.fetch()
        self.assertEqual(send.call_args.args[2]["Authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", json.dumps(run.items))
        self.assertEqual(run.counts(), {"create": 1, "update": 1, "unchanged": 0, "error": 3})
        self.assertFalse(Customer.objects.filter(external_id="1").exists())
        self.assertEqual(Customer.objects.get(external_id="2").name, "Old name")
        results = apply_run(run, self.owner)
        self.assertEqual((results["create"], results["update"], results["error"]), (1, 1, 3))
        created = Customer.objects.get(external_id="1")
        self.assertEqual((created.email, created.source), ("new@example.com", "pull"))
        self.assertEqual(Customer.objects.get(external_id="2").name, "Renamed")
        self.assertIsNone(apply_run(run, self.owner))

    def test_apply_revalidates_changed_data(self):
        run, _ = self.fetch()
        Customer.objects.create(workspace=self.business, name="Late", email="new@example.com")
        results = apply_run(run, self.owner)
        self.assertEqual(results["create"], 0)
        self.assertFalse(Customer.objects.filter(external_id="1").exists())

    def test_fetch_failures_are_recorded(self):
        run, _ = self.fetch(status=401)
        self.assertEqual(run.status, SyncRun.Status.FAILED)
        self.assertIn("401", run.error)
        run, _ = self.fetch(body={"data": {"customers": "nope"}})
        self.assertIn("list path", run.error)
        with patch("integrations.sync.request", return_value=(200, b"not json")):
            self.assertIn("JSON", fetch_preview(self.connector, self.owner).error)


class ScreenTests(IntegrationTestCase):
    def url(self, name, *args):
        return reverse(name, args=[self.business.pk, *args])

    def test_integrations_are_admin_only(self):
        self.client.force_login(self.staff)
        for name in ["integration_keys", "integration_webhooks", "integration_connectors"]:
            self.assertEqual(self.client.get(self.url(name)).status_code, 403)
        self.assertNotContains(self.client.get(reverse("dashboard", args=[self.business.pk])), "Integrations")
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url("integration_keys")).status_code, 404)

    def test_admin_creates_and_revokes_a_key(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url("integration_keys"), {"name": "Website"})
        raw = response.context["secret"]
        key = ApiKey.objects.get(name="Website")
        self.assertEqual(key.digest, digest(raw))
        self.assertNotContains(self.client.get(self.url("integration_keys")), raw)
        self.client.post(self.url("integration_key_revoke", key.pk))
        key.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)

    def test_channel_must_be_on_to_add_integrations(self):
        Workspace.objects.filter(pk=self.business.pk).update(use_api=False)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.url("integration_keys")), "switched off")
        self.client.post(self.url("integration_keys"), {"name": "Website"})
        self.assertFalse(ApiKey.objects.filter(name="Website").exists())

    def test_webhook_url_validation_and_secret_shown_once(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.post(self.url("integration_webhooks"), {"url": "http://shop.example.com/h"}), "https://")
        response = self.client.post(self.url("integration_webhooks"), {"url": "https://shop.example.com/h"})
        endpoint = WebhookEndpoint.objects.get(workspace=self.business)
        self.assertEqual(response.context["secret"], endpoint.secret)
        self.assertNotContains(self.client.get(self.url("integration_webhook", endpoint.pk)), endpoint.secret)

    def test_connector_screens(self):
        self.client.force_login(self.owner)
        self.client.post(
            self.url("integration_connector_create"),
            {"name": "Shop", "url": "https://shop.example.com/c", "list_path": "", "map_external_id": "id", "map_name": "name"},
        )
        connector = Connector.objects.get(workspace=self.business)
        self.assertEqual(connector.mapping, {"external_id": "id", "name": "name"})
        with patch("integrations.sync.request", return_value=(200, b'[{"id": "a", "name": "Ann"}]')):
            response = self.client.post(self.url("integration_connector", connector.pk), {"token": "t"})
        run = SyncRun.objects.get()
        self.assertRedirects(response, self.url("integration_run", run.pk))
        self.assertContains(self.client.get(self.url("integration_run", run.pk)), "Will be added")
        self.client.post(self.url("integration_run_action", run.pk), {"action": "apply"})
        self.assertTrue(Customer.objects.filter(external_id="a", name="Ann").exists())
