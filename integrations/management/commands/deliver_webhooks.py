from django.core.management.base import BaseCommand
from integrations.webhooks import deliver, due


class Command(BaseCommand):
    help = "Send webhook deliveries that are due, including retries."

    def handle(self, *args, **options):
        sent = deliver(due()[:500])
        self.stdout.write(f"Delivered {sent} webhook event(s).")
