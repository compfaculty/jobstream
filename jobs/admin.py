from django.contrib import admin
from django.utils import timezone
from two_factor.admin import AdminSiteOTPRequired

from .models import AuditEvent, Company, IngestionRun, Invitation, Source

admin.site.__class__ = AdminSiteOTPRequired


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("name", "adapter", "enabled", "next_run_at", "last_success_at", "failure_count")
    list_filter = ("enabled", "adapter", "discovery_pending", "authoritative")
    search_fields = ("name", "url", "notes")
    readonly_fields = (
        "last_started_at",
        "last_success_at",
        "failure_count",
        "discovered_from",
        "discovery_url",
    )
    actions = ["schedule_now"]

    @admin.action(description="Schedule selected sources now")
    def schedule_now(self, request, queryset):
        queryset.update(next_run_at=timezone.now())


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "expires_at", "used_at")
    readonly_fields = ("token", "used_at")


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IngestionRun)
class IngestionRunAdmin(ReadOnlyAdmin):
    list_display = (
        "source",
        "started_at",
        "status",
        "imported",
        "rejected",
        "enrichment_failed",
        "error_code",
    )


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("actor", "application_id", "event", "created_at")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "source",
        "website",
        "website_verified",
        "enriched_at",
        "enrichment_error",
    )
    list_filter = ("website_verified", "source")
    search_fields = ("name", "website")
    readonly_fields = (
        "identity_key",
        "logo",
        "logo_source_url",
        "enriched_at",
        "enrichment_attempted_at",
        "enrichment_error",
    )

    def has_add_permission(self, request):
        return False  # Profiles are created by ingestion or the existing-job backfill.
