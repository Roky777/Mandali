import json
from django.core.management.base import BaseCommand, CommandError
from core.models import Workspace
from operations.ledger import run


class Command(BaseCommand):
    help = "Verify that invoice totals, payments, credits, refunds, and gateway evidence agree."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", help="Limit to one workspace ID.")
        parser.add_argument("--json", action="store_true", help="Print machine-readable results.")

    def handle(self, *args, **options):
        workspace = None
        if options["workspace"]:
            workspace = Workspace.objects.filter(pk=options["workspace"]).first()
            if workspace is None:
                raise CommandError("No workspace has that ID.")
        results = run(workspace)
        failed = [r for r in results if r["failures"]]
        if options["json"]:
            self.stdout.write(json.dumps({"ok": not failed, "checks": results}, indent=2))
        else:
            for result in results:
                mark = "FAIL" if result["failures"] else "ok  "
                extra = f" ({result['failures']}: {', '.join(result['examples'])})" if result["failures"] else ""
                self.stdout.write(f"{mark} {result['check']}{extra}")
        if failed:
            raise CommandError(f"{len(failed)} ledger check(s) failed.")
