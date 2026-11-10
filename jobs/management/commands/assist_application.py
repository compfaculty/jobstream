"""Send only an explicitly approved, locally reserved browser application."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from jobs.models import Application
from jobs.services.submissions import submit


class Command(BaseCommand):
    help = "Deliver an approved browser application in visible local Chromium."

    def add_arguments(self, parser):
        parser.add_argument("application_id", type=int)
        parser.add_argument("--owner-id", type=int, required=True)

    def handle(self, *args, **options):
        def notify(page):
            self.stdout.write(
                "Complete the verification prompt in the open Chromium window. "
                "Enter email codes there; do not enter them in this terminal. "
                "The session expires after ten minutes."
            )
            self.stdout.flush()

        try:
            submit(
                options["application_id"],
                assisted_owner_id=options["owner_id"],
                on_human_verification=notify,
            )
        except (Application.DoesNotExist, ValidationError) as exc:
            raise CommandError(
                "Application unavailable or not approved for this owner/mode."
            ) from exc
        application = Application.objects.get(
            pk=options["application_id"], owner_id=options["owner_id"]
        )
        self.stdout.write(f"Application {application.pk}: {application.status}")
        if application.status != Application.Status.CONFIRMED:
            raise CommandError(
                f"Not confirmed: {application.error_code or 'cooldown; try later'}. "
                "Review JobStream before taking further action."
            )
        self.stdout.write(application.portal_receipt_url)
