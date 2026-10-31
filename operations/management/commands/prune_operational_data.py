from datetime import timedelta
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from core.models import AuthRateBucket, Invitation
from integrations.models import IdempotencyRecord, SyncRun, WebhookDelivery
from portal.models import PortalToken

# Retention for operational data. Ledger records, activity, and gateway
# event fingerprints (needed to reject replays) are never pruned here.
RETENTION = {
    "rate_buckets": timedelta(days=2),
    "portal_tokens": timedelta(days=1),
    "invitations": timedelta(days=30),
    "idempotency": timedelta(days=30),
    "webhook_deliveries": timedelta(days=90),
    "sync_previews": timedelta(days=90),
}


def targets(now):
    cut = {name: now - age for name, age in RETENTION.items()}
    return [
        ("rate-limit buckets", AuthRateBucket.objects.filter(touched_at__lt=cut["rate_buckets"]), "delete"),
        ("used or expired portal links", PortalToken.objects.filter(expires_at__lt=cut["portal_tokens"]), "delete"),
        ("expired staff invitations", Invitation.objects.filter(accepted_at=None, expires_at__lt=cut["invitations"]), "delete"),
        ("API idempotency records", IdempotencyRecord.objects.filter(created_at__lt=cut["idempotency"]), "delete"),
        (
            "finished webhook deliveries",
            WebhookDelivery.objects.filter(created_at__lt=cut["webhook_deliveries"]).filter(
                Q(status=WebhookDelivery.Status.DELIVERED) | Q(status=WebhookDelivery.Status.FAILED)
            ),
            "delete",
        ),
        (
            "customer data in old sync previews",
            SyncRun.objects.filter(created_at__lt=cut["sync_previews"]).exclude(items=[]),
            "clear",
        ),
    ]


PAST = {"delete": "deleted", "clear": "cleared"}


class Command(BaseCommand):
    help = "Remove expired operational data. Ledger records and activity are kept."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count without changing anything.")

    def handle(self, *args, **options):
        for label, queryset, action in targets(timezone.now()):
            count = queryset.count()
            if not options["dry_run"] and count:
                if action == "delete":
                    queryset.delete()
                else:
                    queryset.update(items=[])
            verb = f"would {action}" if options["dry_run"] else PAST[action]
            self.stdout.write(f"{label}: {count} {verb}")
        if not options["dry_run"]:
            call_command("clearsessions")
            self.stdout.write("expired sessions: cleared")
