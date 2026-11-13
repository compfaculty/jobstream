"""Create only synthetic mail fixtures; no approval, sending or AI invocation."""

import getpass
import hashlib

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jobs.models import Document, GmailConnection, Job
from jobs.services import sandbox_mail
from jobs.services.resumes import pdf_bytes


class Command(BaseCommand):
    help = "Create a non-staff synthetic mail user, job and PDF; never approve or send."

    def add_arguments(self, parser):
        parser.add_argument("--set-password", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.DEBUG or not settings.SANDBOX_MAIL_ENABLED:
            raise CommandError("Requires DEBUG=1 and SANDBOX_MAIL_ENABLED=1")
        user, created = get_user_model().objects.get_or_create(
            username=sandbox_mail.USERNAME,
            defaults={"email": sandbox_mail.SENDER},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        connection, _ = GmailConnection.objects.get_or_create(
            owner=user,
            defaults={
                "email": sandbox_mail.SENDER,
                "encrypted_credentials": sandbox_mail.MARKER,
            },
        )
        sandbox_mail.validate(connection)
        if options["set_password"]:
            password = getpass.getpass("Synthetic account password: ")
            if not password or password != getpass.getpass("Repeat password: "):
                raise CommandError("Passwords must be nonempty and match")
            from django.contrib.auth.password_validation import validate_password

            validate_password(password, user)
            user.set_password(password)
            user.save(update_fields=["password"])
        job, _ = Job.objects.get_or_create(
            canonical_url=sandbox_mail.JOB_URL,
            defaults={
                "title": "[SYNTHETIC TEST] Python Engineer",
                "company": "Synthetic employer — no real applications",
                "description": "Synthetic email delivery test. Python and Linux.",
                "apply_email": sandbox_mail.RECIPIENT,
            },
        )
        document = Document.objects.filter(owner=user, name="SYNTHETIC_TEST_CV.pdf").first()
        if document is None:
            data = pdf_bytes(
                "SYNTHETIC TEST APPLICANT — NOT A REAL PERSON",
                sandbox_mail.SENDER,
                [{"section": "Skills", "text": "Synthetic Python and Linux test experience."}],
            )
            document = Document(
                owner=user,
                name="SYNTHETIC_TEST_CV.pdf",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
            document.file.save("synthetic-cv.pdf", ContentFile(data), save=False)
            document.save()
        self.stdout.write(
            f"Synthetic user ready; job={job.pk}, document={document.pk}. "
            "No application approved or sent. Use --set-password for browser login."
        )
