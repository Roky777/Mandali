import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from unittest import skipUnless
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet
from django.core import mail
from django.core.management import call_command
from django.db import (
    IntegrityError,
    transaction,
    connection,
    connections,
    close_old_connections,
)
from django.test import (
    TestCase,
    TransactionTestCase,
    Client,
    override_settings,
    SimpleTestCase,
)
from django.urls import reverse
from django.utils import timezone
from billing import services as billing
from billing.models import (
    Customer,
    Product,
    Plan,
    Tax,
    Subscription,
    Invoice,
    Payment,
    Discount,
)
from checkout import services as checkout
from checkout.crypto import CheckoutError
from checkout.gateways import GatewayError, GatewayRejected
from core.models import Workspace, User, Membership
from .calendar import advance
from .models import Schedule, Cycle, Mandate, Capture, Notice, JobLease
from .forms import ScheduleForm
from .gateway import RecurringGateway
from . import services, collection, notices, worker


@override_settings(
    PAYMENT_ENCRYPTION_KEYS=Fernet.generate_key().decode(),
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RecurringTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            "recurring@example.com", "Local-test!Owner42"
        )
        cls.staff = User.objects.create_user("staff@example.com", "Local-test!Staff42")
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
        cls.customer = Customer.objects.create(
            workspace=cls.workspace,
            name="Example Buyer",
            email="buyer@example.com",
            billing_address="Example address",
        )
        cls.tax = Tax.objects.create(workspace=cls.workspace, name="GST", rate=18)
        cls.product = Product.objects.create(
            workspace=cls.workspace, name="Service", sales_price=1000, tax=cls.tax
        )
        cls.plan = Plan.objects.create(
            workspace=cls.workspace,
            name="Monthly",
            price=1000,
            period="monthly",
            can_pause=True,
            can_renew=True,
            auto_close=True,
        )
        cls.account = checkout.configure_gateway(
            cls.workspace,
            cls.owner,
            {
                "key_id": "rzp_test_recurring",
                "key_secret": "test-recurring-secret-only",
                "webhook_secret": "test-webhook-secret-only",
            },
        )

    def sub(self, first=None, expiry=None):
        first = first or timezone.localdate()
        sub = Subscription.objects.create(
            workspace=self.workspace,
            customer=self.customer,
            plan=self.plan,
            number=billing.next_number(self.workspace, "subscription", "SUB"),
            start_date=first,
            expiry_date=expiry,
            created_by=self.owner,
            payment_terms_days=2,
        )
        billing.add_subscription_line(sub, {"product": self.product}, 1)
        Subscription.objects.filter(pk=sub.pk).update(status="active")
        sub.refresh_from_db()
        return sub

    def data(self, sub, automatic=False, **kwargs):
        return {
            "mode": "automatic" if automatic else "invoice",
            "first_bill_on": sub.start_date,
            "is_sandbox": automatic,
            "reminders": True,
            "discount": None,
            "total_count": 12,
            "confirm": True,
            **kwargs,
        }

    def schedule(self, first=None, expiry=None, **kwargs):
        sub = self.sub(first=first, expiry=expiry)
        return services.configure(sub, self.owner, self.data(sub, **kwargs))

    def automatic(self):
        sub = self.sub(first=timezone.localdate() + timedelta(days=2))
        schedule = services.configure(sub, self.owner, self.data(sub, automatic=True))
        with patch.object(
            RecurringGateway,
            "create_plan",
            autospec=True,
            side_effect=lambda g, m: self.plan_entity(m),
        ), patch.object(
            RecurringGateway,
            "create_subscription",
            autospec=True,
            side_effect=lambda g, m: self.sub_entity(m),
        ):
            return collection.prepare(schedule, self.owner)

    def plan_entity(self, mandate):
        return {
            "id": "plan_example123",
            "notes": {"mandali_mandate": str(mandate.pk)},
            "period": mandate.schedule.period,
            "interval": 1,
            "item": {"amount": mandate.amount_paise, "currency": "INR"},
        }

    def sub_entity(self, mandate, status="created"):
        return {
            "id": "sub_" + mandate.pk.hex,
            "plan_id": mandate.plan_id or "plan_example123",
            "status": status,
            "notes": {"mandali_mandate": str(mandate.pk)},
            "quantity": 1,
            "total_count": mandate.total_count,
            "start_at": mandate.start_at,
            "short_url": "https://rzp.io/i/example",
        }

    def evidence(
        self,
        mandate,
        start=None,
        payment_id="pay_recurring123",
        invoice_id="inv_recurring123",
    ):
        start = start or mandate.schedule.anchor_on
        end = advance(start, mandate.schedule.period, mandate.schedule.anchor_on)
        stamp = lambda day: int(
            datetime.combine(day, time(), ZoneInfo("Asia/Kolkata")).timestamp()
        )
        invoice = {
            "id": invoice_id,
            "subscription_id": mandate.subscription_id,
            "payment_id": payment_id,
            "order_id": "order_recurring123",
            "currency": "INR",
            "amount": mandate.amount_paise,
            "amount_paid": mandate.amount_paise,
            "status": "paid",
            "billing_start": stamp(start),
            "billing_end": stamp(end),
        }
        payment = {
            "id": payment_id,
            "invoice_id": invoice_id,
            "order_id": "order_recurring123",
            "amount": mandate.amount_paise,
            "currency": "INR",
            "status": "captured",
            "captured": True,
            "amount_refunded": 0,
        }
        return invoice, payment

    @contextmanager
    def provider(self, mandate, invoices=None, payment=None, status="active"):
        with patch.object(
            RecurringGateway,
            "fetch_subscription",
            return_value=self.sub_entity(mandate, status),
        ), patch.object(
            RecurringGateway, "fetch_plan", return_value=self.plan_entity(mandate)
        ), patch.object(
            RecurringGateway, "invoices", return_value=invoices or []
        ), patch.object(
            RecurringGateway, "fetch_payment", return_value=payment or {}
        ):
            yield

    def test_calendar_month_end_and_leap_anchor(self):
        anchor = date(2024, 1, 31)
        self.assertEqual(advance(anchor, "monthly", anchor), date(2024, 2, 29))
        self.assertEqual(
            advance(date(2024, 2, 29), "monthly", anchor), date(2024, 3, 31)
        )
        self.assertEqual(
            advance(date(2025, 2, 28), "yearly", date(2024, 2, 29)), date(2026, 2, 28)
        )
        self.assertEqual(
            advance(date(2027, 2, 28), "yearly", date(2024, 2, 29)), date(2028, 2, 29)
        )
        self.assertEqual(advance(anchor, "daily", anchor), date(2024, 2, 1))
        self.assertEqual(advance(anchor, "weekly", anchor), date(2024, 2, 7))

    def test_due_invoice_once_with_immutable_cycle_prices_and_snapshots(self):
        schedule = self.schedule()
        self.product.sales_price = 2000
        self.product.save()
        first = services.bill_one(schedule.pk)
        self.assertEqual(first.invoice.total, Decimal("1180"))
        self.assertEqual(first.invoice.customer_name, "Example Buyer")
        self.assertEqual(first.invoice.issue_date, timezone.localdate())
        self.assertIsNone(services.bill_one(schedule.pk))
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Notice.objects.count(), 1)
        self.assertEqual(Cycle.objects.count(), 1)

    def test_invoice_automation_is_opt_in(self):
        self.sub()
        self.assertEqual(worker.run()["schedules"], 0)
        self.assertFalse(Invoice.objects.exists())

    def test_expiry_is_inclusive_truncated_and_auto_closed(self):
        today = timezone.localdate()
        schedule = self.schedule(expiry=today)
        cycle = services.bill_one(schedule.pk)
        self.assertEqual(cycle.ends_before, today + timedelta(days=1))
        self.assertIsNone(services.bill_one(schedule.pk, today + timedelta(days=2)))
        schedule.subscription.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(schedule.subscription.status, "closed")
        self.assertFalse(schedule.enabled)

    def test_pause_resume_skips_boundaries_without_back_charging(self):
        schedule = self.schedule(first=date(2024, 1, 31))
        billing.transition(schedule.subscription, "pause", self.owner)
        self.assertIsNone(services.bill_one(schedule.pk))
        billing.transition(schedule.subscription, "resume", self.owner)
        schedule.refresh_from_db()
        self.assertGreaterEqual(schedule.next_bill_on, timezone.localdate())
        self.assertFalse(Invoice.objects.exists())

    def test_plan_pause_and_renew_policies(self):
        schedule = self.schedule()
        self.plan.can_pause = False
        self.plan.can_renew = False
        self.plan.save()
        with self.assertRaises(billing.BillingError):
            billing.transition(schedule.subscription, "pause", self.owner)
        with self.assertRaises(billing.BillingError):
            services.renew(
                schedule.subscription,
                self.owner,
                timezone.localdate() + timedelta(days=90),
            )

    def test_renewal_extends_closed_term_without_backlog(self):
        schedule = self.schedule(first=date(2024, 1, 31), expiry=date(2024, 2, 10))
        Schedule.objects.filter(pk=schedule.pk).update(
            enabled=False, next_bill_on=date(2024, 2, 11)
        )
        Subscription.objects.filter(pk=schedule.subscription_id).update(status="closed")
        services.renew(
            schedule.subscription,
            self.owner,
            timezone.localdate() + timedelta(days=100),
        )
        schedule.refresh_from_db()
        schedule.subscription.refresh_from_db()
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.subscription.status, "active")
        self.assertGreaterEqual(schedule.next_bill_on, timezone.localdate())
        with self.assertRaises(billing.BillingError):
            services.renew(schedule.subscription, self.owner, date(2024, 3, 1))

    def test_discount_eligible_then_exhausted_and_expired(self):
        discount = Discount.objects.create(
            workspace=self.workspace, name="Once", value=10, usage_limit=1
        )
        schedule = self.schedule(discount=discount)
        cycle = services.bill_one(schedule.pk)
        self.assertEqual(cycle.invoice.total, Decimal("1062"))
        discount.refresh_from_db()
        self.assertEqual(discount.times_used, 1)
        schedule.refresh_from_db()
        later = services.bill_one(schedule.pk, schedule.next_bill_on)
        self.assertEqual(later.invoice.total, Decimal("1180"))
        self.assertIsNone(later.invoice.discount_id)

    def test_duplicate_schedule_and_manual_cycle_draft_are_blocked(self):
        schedule = self.schedule()
        with self.assertRaises(billing.BillingError):
            services.configure(
                schedule.subscription, self.owner, self.data(schedule.subscription)
            )
        with self.assertRaises(billing.BillingError):
            billing.invoice_from_subscription(schedule.subscription, self.owner)
        cycle = services.bill_one(schedule.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Cycle.objects.create(
                schedule=schedule,
                starts_on=cycle.starts_on,
                ends_before=cycle.ends_before,
                invoice=Invoice.objects.create(
                    workspace=self.workspace,
                    customer=self.customer,
                    created_by=self.owner,
                ),
            )

    def test_catchup_bounded_and_continues(self):
        self.plan.period = "daily"
        self.plan.save()
        schedule = self.schedule(
            first=timezone.localdate() - timedelta(days=25), reminders=False
        )
        worker.run()
        self.assertEqual(Cycle.objects.count(), 12)
        worker.run()
        self.assertEqual(Cycle.objects.count(), 24)
        worker.run()
        self.assertEqual(Cycle.objects.count(), 26)
        worker.run()
        self.assertEqual(Cycle.objects.count(), 26)

    def test_job_lease_prevents_overlap(self):
        JobLease.objects.create(
            name="billing", expires_at=timezone.now() + timedelta(minutes=10)
        )
        self.assertTrue(worker.run()["busy"])

    def test_job_command_outputs_counts(self):
        out = StringIO()
        call_command("run_recurring", limit=1, stdout=out)
        self.assertEqual(json.loads(out.getvalue())["errors"], 0)

    def test_reminders_once_and_paid_cancelled_or_disabled_are_skipped(self):
        cycle = services.bill_one(self.schedule().pk)
        self.assertEqual(notices.deliver(), 1)
        self.assertEqual(len(mail.outbox), 1)
        notices.deliver()
        self.assertEqual(len(mail.outbox), 1)
        services.queue_due_notices(cycle.invoice.due_date)
        services.queue_due_notices(cycle.invoice.due_date)
        self.assertEqual(Notice.objects.filter(kind="due").count(), 1)
        services.queue_due_notices(cycle.invoice.due_date + timedelta(days=3))
        self.assertEqual(Notice.objects.filter(kind="overdue").count(), 1)
        billing.record_payment(
            cycle.invoice, self.owner, cycle.invoice.total, "bank", timezone.localdate()
        )
        notices.deliver()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Notice.objects.filter(status="skipped").count(), 2)

    def test_email_failure_and_interruption_are_uncertain_not_auto_retried(self):
        services.bill_one(self.schedule().pk)
        with patch("recurring.notices.EmailMessage.send", side_effect=TimeoutError()):
            notices.deliver()
        self.assertEqual(Notice.objects.get().status, "unknown")
        notices.deliver()
        self.assertEqual(len(mail.outbox), 0)
        Notice.objects.update(
            status="sending", updated_at=timezone.now() - timedelta(minutes=20)
        )
        notices.deliver()
        self.assertEqual(Notice.objects.get().status, "unknown")

    def test_missing_customer_email_is_skipped(self):
        self.customer.email = ""
        self.customer.save()
        services.bill_one(self.schedule().pk)
        notices.deliver()
        self.assertEqual(Notice.objects.get().status, "skipped")

    def test_tenant_and_admin_boundaries(self):
        schedule = self.schedule()
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(
                reverse("recurring_detail", args=[self.workspace.pk, schedule.pk])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "recurring_setup",
                    args=[self.workspace.pk, schedule.subscription_id],
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("recurring_action", args=[self.workspace.pk, schedule.pk]),
                {"action": "prepare"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse("recurring_detail", args=[self.foreign.pk, schedule.pk])
            ).status_code,
            404,
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(
                reverse("recurring_detail", args=[self.foreign.pk, schedule.pk])
            ).status_code,
            404,
        )

    def test_email_retry_requires_admin_explicit_confirmation(self):
        cycle = services.bill_one(self.schedule().pk)
        notice = Notice.objects.get()
        Notice.objects.update(status="unknown")
        url = reverse("recurring_notice_retry", args=[self.workspace.pk, notice.pk])
        self.client.force_login(self.owner)
        self.client.post(url, {})
        notice.refresh_from_db()
        self.assertEqual(notice.status, "unknown")
        self.client.post(url, {"confirm": "retry"})
        notice.refresh_from_db()
        self.assertEqual(notice.status, "queued")

    def test_automatic_form_rejects_daily_live_past_discount_and_expiry(self):
        sub = self.sub(first=timezone.localdate() + timedelta(days=2))
        data = self.data(sub, automatic=True)
        self.assertTrue(ScheduleForm(data, subscription=sub).is_valid())
        self.assertFalse(
            ScheduleForm({**data, "is_sandbox": False}, subscription=sub).is_valid()
        )
        self.assertFalse(
            ScheduleForm(
                {**data, "first_bill_on": timezone.localdate()}, subscription=sub
            ).is_valid()
        )
        self.plan.period = "daily"
        self.plan.save()
        sub.plan = self.plan
        self.assertFalse(ScheduleForm(data, subscription=sub).is_valid())

    def test_authorization_is_not_a_payment_and_link_is_encrypted(self):
        mandate = self.automatic()
        self.assertEqual(mandate.status, "created")
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertNotIn("rzp.io", mandate.authorization_ciphertext)
        self.assertEqual(
            collection.authorization_url(mandate), "https://rzp.io/i/example"
        )
        with patch.object(RecurringGateway, "create_subscription") as create:
            collection.prepare(mandate.schedule, self.owner)
        create.assert_not_called()

    def test_authorized_capture_settles_cycle_once(self):
        mandate = self.automatic()
        invoice, payment = self.evidence(mandate)
        with self.provider(mandate, [invoice], payment):
            collection.reconcile(mandate)
            collection.reconcile(mandate)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Cycle.objects.count(), 1)
        self.assertEqual(Invoice.objects.get().status, "paid")
        self.assertTrue(Invoice.objects.get().is_sandbox)
        self.assertEqual(
            billing.billing_summary(self.workspace, timezone.localdate())[
                "outstanding"
            ],
            Decimal("0"),
        )

    def test_authorization_only_provider_invoice_is_not_a_billing_cycle(self):
        mandate = self.automatic()
        invoice, payment = self.evidence(mandate)
        invoice["billing_start"] = None
        invoice["billing_end"] = None
        with self.provider(mandate, [invoice], payment):
            collection.reconcile(mandate)
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(Capture.objects.exists())

    def test_bad_capture_evidence_is_reviewed_without_payment(self):
        for field, value in [
            ("amount", 1),
            ("currency", "USD"),
            ("captured", False),
            ("amount_refunded", 1),
            ("order_id", "order_foreign"),
        ]:
            with self.subTest(field=field):
                mandate = self.automatic()
                invoice, payment = self.evidence(
                    mandate,
                    payment_id="pay_" + mandate.pk.hex,
                    invoice_id="inv_" + mandate.pk.hex,
                )
                payment[field] = value
                result = collection.apply_invoice(mandate, invoice, payment)
                self.assertEqual(result.status, "review")
                self.assertFalse(result.payment_id)
        self.assertFalse(Payment.objects.exists())

    def test_overlapping_or_extra_cycles_are_reviewed(self):
        mandate = self.automatic()
        invoice, payment = self.evidence(mandate)
        collection.apply_invoice(mandate, invoice, payment)
        invoice, payment = self.evidence(
            mandate,
            start=mandate.schedule.anchor_on + timedelta(days=1),
            payment_id="pay_second",
            invoice_id="inv_second",
        )
        self.assertEqual(
            collection.apply_invoice(mandate, invoice, payment).status, "review"
        )
        self.assertEqual(Payment.objects.count(), 1)

    def test_creation_timeout_never_creates_a_second_subscription(self):
        sub = self.sub(first=timezone.localdate() + timedelta(days=2))
        schedule = services.configure(sub, self.owner, self.data(sub, automatic=True))
        with patch.object(
            RecurringGateway,
            "create_plan",
            autospec=True,
            side_effect=lambda g, m: self.plan_entity(m),
        ), patch.object(
            RecurringGateway,
            "create_subscription",
            side_effect=GatewayError("Uncertain"),
        ) as create:
            mandate = collection.prepare(schedule, self.owner)
            collection.prepare(schedule, self.owner)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(mandate.status, "unknown")
        with self.provider(mandate):
            recovered = collection.reconcile(
                mandate, subscription_id="sub_" + mandate.pk.hex
            )
        self.assertEqual(recovered.status, "active")

    def test_provider_actions_must_confirm_remote_state(self):
        mandate = self.automatic()
        with patch.object(
            RecurringGateway, "change", side_effect=GatewayError("Uncertain")
        ):
            with self.assertRaises(CheckoutError):
                collection.change(mandate, self.owner, "cancel")
        mandate.refresh_from_db()
        self.assertEqual(mandate.pending_action, "cancel")
        mandate.schedule.subscription.refresh_from_db()
        self.assertEqual(mandate.schedule.subscription.status, "active")
        with self.provider(mandate, status="cancelled"):
            collection.reconcile(mandate)
        mandate.refresh_from_db()
        self.assertEqual(mandate.pending_action, "")
        mandate.schedule.subscription.refresh_from_db()
        self.assertEqual(mandate.schedule.subscription.status, "closed")

    def test_local_transition_and_renewal_cannot_replace_provider_mandate(self):
        mandate = self.automatic()
        for action in ("pause", "close"):
            with self.assertRaises(billing.BillingError):
                billing.transition(mandate.schedule.subscription, action, self.owner)
        with self.assertRaises(billing.BillingError):
            services.renew(
                mandate.schedule.subscription,
                self.owner,
                timezone.localdate() + timedelta(days=365),
            )

    def test_signed_webhook_uses_provider_state_and_is_replay_safe(self):
        mandate = self.automatic()
        invoice, payment = self.evidence(mandate)
        payload = {
            "event": "subscription.charged",
            "payload": {
                "subscription": {"entity": self.sub_entity(mandate, "active")},
                "payment": {"entity": {"id": payment["id"]}},
            },
        }
        body = json.dumps(payload).encode()
        signature = hmac.new(
            b"test-webhook-secret-only", body, hashlib.sha256
        ).hexdigest()
        with self.provider(mandate, [invoice], payment):
            self.assertEqual(
                checkout.process_webhook(
                    self.account, body, signature, "evt_recurring"
                ),
                "active",
            )
            self.assertEqual(
                checkout.process_webhook(
                    self.account, body, signature, "evt_recurring"
                ),
                "duplicate",
            )
        self.assertEqual(Payment.objects.count(), 1)
        with patch.object(RecurringGateway, "fetch_subscription") as fetch:
            with self.assertRaises(GatewayError):
                checkout.process_webhook(self.account, body, "bad", "evt_bad")
        fetch.assert_not_called()

    def test_provider_foreign_or_changed_plan_is_rejected(self):
        mandate = self.automatic()
        entity = self.sub_entity(mandate)
        entity["notes"] = {"mandali_mandate": "foreign"}
        with self.assertRaises(GatewayError):
            collection.save_subscription(mandate, entity)
        plan = self.plan_entity(mandate)
        plan["item"]["amount"] += 1
        with self.assertRaises(GatewayError):
            collection.plan_evidence(mandate, plan)

    def test_worker_never_generates_manual_invoices_for_automatic_mode(self):
        mandate = self.automatic()
        with self.provider(mandate):
            worker.run()
        self.assertFalse(Invoice.objects.exists())

    def test_csrf_guards_setup_and_provider_actions(self):
        schedule = self.schedule()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(
                reverse("recurring_action", args=[self.workspace.pk, schedule.pk]),
                {"action": "prepare"},
            ).status_code,
            403,
        )

    def test_new_screens_and_invoice_period_render(self):
        schedule = self.schedule()
        cycle = services.bill_one(schedule.pk)
        self.client.force_login(self.owner)
        for name, args in [
            ("recurring_overview", [self.workspace.pk]),
            ("recurring_detail", [self.workspace.pk, schedule.pk]),
            ("subscription_detail", [self.workspace.pk, schedule.subscription_id]),
            ("invoice_detail", [self.workspace.pk, cycle.invoice_id]),
            ("invoice_print", [self.workspace.pk, cycle.invoice_id]),
        ]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_renewing_a_paused_term_does_not_reactivate_or_back_charge(self):
        schedule = self.schedule(first=date(2024, 1, 31))
        billing.transition(schedule.subscription, "pause", self.owner)
        services.renew(
            schedule.subscription, self.owner, timezone.localdate() + timedelta(days=90)
        )
        schedule.subscription.refresh_from_db()
        self.assertEqual(schedule.subscription.status, "paused")
        self.assertIsNone(services.bill_one(schedule.pk))
        self.assertFalse(Invoice.objects.exists())
        billing.transition(schedule.subscription, "resume", self.owner)
        schedule.refresh_from_db()
        self.assertGreaterEqual(schedule.next_bill_on, timezone.localdate())

    def test_truncated_expiry_keeps_original_calendar_boundary(self):
        schedule = self.schedule(first=date(2024, 1, 31), expiry=date(2024, 2, 10))
        cycle = services.bill_one(schedule.pk, date(2024, 1, 31))
        schedule.refresh_from_db()
        self.assertEqual(cycle.ends_before, date(2024, 2, 11))
        self.assertEqual(schedule.next_bill_on, date(2024, 2, 29))

    def test_definitive_provider_action_rejection_does_not_poison_recovery(self):
        mandate = self.automatic()
        with patch.object(
            RecurringGateway, "change", side_effect=GatewayRejected("Rejected")
        ):
            with self.assertRaises(GatewayRejected):
                collection.change(mandate, self.owner, "cancel")
        mandate.refresh_from_db()
        self.assertEqual(mandate.pending_action, "")
        with self.provider(mandate, status="cancelled"), patch.object(
            RecurringGateway, "change"
        ):
            collection.change(mandate, self.owner, "cancel")
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, "cancelled")

    def test_unpaid_review_capture_can_reconcile_after_reference_conflict_is_resolved(
        self,
    ):
        mandate = self.automatic()
        entity, evidence = self.evidence(mandate)
        other = billing.invoice_from_subscription(self.sub(), self.owner)
        billing.issue_invoice(other, self.owner)
        offline = billing.record_payment(
            other,
            self.owner,
            other.total,
            "bank",
            timezone.localdate(),
            reference=evidence["id"],
        )
        result = collection.apply_invoice(mandate, entity, evidence)
        self.assertEqual(result.status, "review")
        billing.reverse_payment(offline, self.owner, "Incorrect reference")
        recovered = collection.apply_invoice(mandate, entity, evidence)
        self.assertEqual(recovered.status, "paid")
        self.assertEqual(Capture.objects.count(), 1)
        self.assertEqual(Cycle.objects.count(), 1)

    def test_stale_provider_response_cannot_reopen_cancelled_mandate(self):
        mandate = self.automatic()
        collection.save_subscription(mandate, self.sub_entity(mandate, "cancelled"))
        with self.assertRaises(GatewayError):
            collection.save_subscription(mandate, self.sub_entity(mandate, "active"))
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, "cancelled")

    def test_pending_provider_cycle_is_visible_and_cannot_start_fallback_collection(
        self,
    ):
        mandate = self.automatic()
        entity, evidence = self.evidence(mandate)
        entity.update(status="issued", payment_id=None, amount_paid=0)
        with self.provider(mandate, [entity], status="pending"):
            collection.reconcile(mandate)
        cycle = Cycle.objects.get()
        self.assertEqual(cycle.invoice.status, "issued")
        self.assertFalse(Payment.objects.exists())
        with self.assertRaises(billing.BillingError):
            billing.record_payment(
                cycle.invoice,
                self.owner,
                cycle.invoice.total,
                "bank",
                timezone.localdate(),
            )
        with self.assertRaises(billing.BillingError):
            billing.cancel_invoice(cycle.invoice, self.owner, "local cancel")
        with self.assertRaises(CheckoutError):
            checkout.create_link(
                self.workspace,
                self.owner,
                {
                    "invoice": cycle.invoice,
                    "title": "Unsafe fallback",
                    "quantity": 1,
                    "valid_days": 7,
                },
            )
        entity, evidence = self.evidence(mandate)
        with self.provider(mandate, [entity], evidence):
            collection.reconcile(mandate)
        self.assertEqual(Cycle.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        cycle.invoice.refresh_from_db()
        self.assertEqual(cycle.invoice.status, "paid")

    def test_authorization_controls_follow_verified_state(self):
        mandate = self.automatic()
        self.client.force_login(self.owner)
        url = reverse("recurring_detail", args=[self.workspace.pk, mandate.schedule_id])
        response = self.client.get(url)
        self.assertNotContains(response, "Request pause")
        self.assertNotContains(response, "Request resume")
        self.assertContains(response, "Cancel test collection")
        collection.save_subscription(mandate, self.sub_entity(mandate, "cancelled"))
        response = self.client.get(url)
        self.assertNotContains(response, "Cancel test collection")
        self.assertNotContains(response, "Open test authorization")

    def test_history_pagination_includes_later_pages_and_rejects_bad_items(self):
        mandate = self.automatic()
        gateway = RecurringGateway(mandate.gateway)
        first = [{"id": "inv_" + str(i)} for i in range(20)]
        with patch.object(
            gateway,
            "request",
            side_effect=[{"items": first}, {"items": [{"id": "inv_later"}]}],
        ) as request:
            self.assertEqual(len(gateway.invoices(mandate.subscription_id)), 21)
        self.assertIn("skip=20", request.call_args.args[1])
        with patch.object(gateway, "request", return_value={"items": [None]}):
            with self.assertRaises(GatewayError):
                gateway.invoices(mandate.subscription_id)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row-lock concurrency")
@override_settings(PAYMENT_ENCRYPTION_KEYS=Fernet.generate_key().decode())
class RecurringConcurrencyTests(TransactionTestCase):
    sub = RecurringTests.sub
    data = RecurringTests.data
    schedule = RecurringTests.schedule
    automatic = RecurringTests.automatic
    plan_entity = RecurringTests.plan_entity
    sub_entity = RecurringTests.sub_entity
    evidence = RecurringTests.evidence

    def setUp(self):
        RecurringTests.setUpTestData.__func__(type(self))

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

    def test_concurrent_jobs_issue_one_period_invoice(self):
        schedule = self.schedule()
        self.parallel(lambda: services.bill_one(schedule.pk))
        self.assertEqual(Cycle.objects.count(), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Notice.objects.count(), 1)

    def test_concurrent_provider_evidence_settles_once(self):
        mandate = self.automatic()
        invoice, payment = self.evidence(mandate)
        self.parallel(
            lambda: collection.apply_invoice(
                Mandate.objects.get(pk=mandate.pk), invoice, payment
            )
        )
        self.assertEqual(Cycle.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Capture.objects.count(), 1)

    def test_concurrent_notice_delivery_claims_once(self):
        services.bill_one(self.schedule().pk)
        with patch("recurring.notices.EmailMessage.send", return_value=1) as send:
            self.parallel(lambda: notices.deliver())
        self.assertEqual(send.call_count, 1)
        self.assertEqual(Notice.objects.get().status, "sent")


class BillingJobDeploymentTests(SimpleTestCase):
    def test_worker_uses_runtime_identity_secrets_and_single_task(self):
        from pathlib import Path
        from scripts.deploy_recurring_job import stage_job

        config = json.loads(Path("deploy/google-cloud/config.example.json").read_text())
        config["image_uri"] = (
            "asia-south1-docker.pkg.dev/your-project-id/mandali/web@sha256:" + "a" * 64
        )
        config["base_url"] = "https://mandali.example.com"
        observed = {}

        def fake(*args, **kwargs):
            observed["args"] = args
            observed["env"] = json.loads(
                Path(args[args.index("--env-vars-file") + 1]).read_text()
            )

        with patch("scripts.deploy_recurring_job.gcloud", side_effect=fake), patch(
            "builtins.print"
        ):
            stage_job(config)
        args = observed["args"]
        self.assertEqual(args[:3], ("run", "jobs", "deploy"))
        self.assertEqual(
            args[args.index("--service-account") + 1], config["app_service_account"]
        )
        self.assertIn(
            "PAYMENT_ENCRYPTION_KEYS=mandali-payment-encryption:1",
            args[args.index("--set-secrets") + 1],
        )
        self.assertNotIn("DJANGO_TRUST_CLOUD_RUN_PROXY", observed["env"])
        self.assertEqual(args[args.index("--tasks") + 1], "1")
        self.assertEqual(args[args.index("--max-retries") + 1], "0")
        self.assertIn("run_recurring", args[args.index("--args") + 1])
