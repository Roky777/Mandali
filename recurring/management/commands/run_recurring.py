import json
from django.core.management.base import BaseCommand, CommandError
from recurring.worker import run


class Command(BaseCommand):
    help = "Generate due invoices, reconcile authorized Test collections, and deliver queued reminders."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("Use a limit between 1 and 500.")
        result = run(options["limit"])
        self.stdout.write(json.dumps(result))
        if result["errors"]:
            raise CommandError(
                "Some schedules need review. See Automation for details."
            )
