"""Explicit, offline pilot reset. Never resets identity sequences or budget headroom."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from jobs.models import AICostMonth, AIQuota, Company, CompanyAlias, Source


class Command(BaseCommand):
    help = "Dry-run a pilot reset; --apply requires --admin, --backup-manifest and --quiesced."

    def add_arguments(self, parser):
        parser.add_argument("--admin", required=True, help="Exact admin username")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--quiesced", action="store_true")
        parser.add_argument("--backup-manifest")

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            admin = User.objects.get(username=options["admin"], is_superuser=True, is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("An exact active superuser is required.") from exc
        keep = {Source, Company, CompanyAlias, AICostMonth, AIQuota}
        clear = [m for m in apps.get_app_config("jobs").get_models() if m not in keep]
        report = {
            "apply": options["apply"],
            "preserve_admin_id": admin.pk,
            "sources": Source.objects.count(),
            "delete": {m._meta.label: m.objects.count() for m in clear},
            "other_users": User.objects.exclude(pk=admin.pk).count(),
        }
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not options["apply"]:
            return
        if not options["quiesced"] or not options["backup_manifest"]:
            raise CommandError(
                "Apply requires a verified backup manifest and stopped writers (--quiesced)."
            )
        manifest_path = Path(options["backup_manifest"])
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get("restore_verified") or manifest.get("admin_id") != admin.pk:
            raise CommandError("Backup restore verification and matching admin are required.")
        dump = manifest_path.parent / manifest["database_file"]
        if hashlib.sha256(dump.read_bytes()).hexdigest() != manifest["database_sha256"]:
            raise CommandError("Database backup checksum mismatch.")
        files = manifest["files"]
        for row in files:
            path = manifest_path.parent / row["backup"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise CommandError("Private file backup checksum mismatch.")
        if Source.objects.filter(lease_until__gt=timezone.now()).exists():
            raise CommandError("Source work is still leased; drain workers first.")
        from jobs.models import AgentExecution, Application, CVRepresentation, NormalizationBatch

        if (
            AgentExecution.objects.filter(status__in=["running", "queued"]).exists()
            or NormalizationBatch.objects.exclude(
                state__in=["completed", "failed", "expired", "cancelled"]
            ).exists()
            or CVRepresentation.objects.filter(
                state="processing", lease_until__gt=timezone.now()
            ).exists()
            or Application.objects.filter(status__in=["submitting", "approved"]).exists()
        ):
            raise CommandError("Outstanding work must be reconciled before resetting.")
        now = timezone.now()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(720316003)")
                tables = ", ".join(connection.ops.quote_name(m._meta.db_table) for m in clear)
                cursor.execute("TRUNCATE TABLE " + tables)
                cursor.execute("SELECT to_regclass('infinity_runtime.executions')")
                if cursor.fetchone()[0]:
                    cursor.execute("DELETE FROM infinity_runtime.provider_calls")
                    cursor.execute("DELETE FROM infinity_runtime.executions")
                    cursor.execute("DELETE FROM infinity_runtime.threads")
            User.objects.exclude(pk=admin.pk).delete()
            from django.contrib.sessions.models import Session

            Session.objects.all().delete()
            from django.contrib.admin.models import LogEntry

            LogEntry.objects.all().delete()
            for model in apps.get_app_config("axes").get_models():
                model.objects.all().delete()
            required = set(
                Source.objects.exclude(company_id=None).values_list("company_id", flat=True)
            )
            # Preserve source-linked company identities, not enriched company marketing/files.
            Company.objects.exclude(pk__in=required).delete()
            Company.objects.all().update(
                description="",
                logo="",
                logo_source_url="",
                enriched_at=None,
                enrichment_attempted_at=None,
                enrichment_error="",
                discovery_error="",
            )
            for month in AICostMonth.objects.all():
                month.carried_usd = month.committed_usd
                month.save(update_fields=["carried_usd"])
            AIQuota.objects.exclude(day=now.date()).delete()
            for index, source in enumerate(Source.objects.order_by("pk")):
                source.inventory_token = source.inventory_started_at = source.lease_token = (
                    source.lease_until
                ) = None
                source.inventory_cursor, source.inventory_config = {}, ""
                source.last_complete_at = source.last_started_at = source.last_success_at = None
                source.failure_count, source.last_error = 0, ""
                source.next_run_at = source.next_dispatch_at = now + timedelta(minutes=5 + index)
                source.save()
        # Storage deletion follows the DB commit; rerunning this manifest's file cleanup is safe.
        from django.core.files.storage import default_storage

        for row in files:
            default_storage.delete(row["name"])
        self.stdout.write("Reset complete. Sessions cleared; admin must sign in with existing MFA.")
