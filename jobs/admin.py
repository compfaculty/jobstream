from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from two_factor.admin import AdminSiteOTPRequired

from .admin_filters import CompanyFilter, LocationFilter, TechnologyFilter, TechnologyScopeFilter
from .models import (
    AuditEvent,
    Company,
    CompanyAlias,
    CompanyProvenance,
    IngestionRun,
    Invitation,
    Job,
    JobEvent,
    JobOrigin,
    Source,
)
from .services.catalogue import import_companies, new_identity, verify_company
from .services.network import canonical_url

admin.site.__class__ = AdminSiteOTPRequired


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "adapter",
        "enabled",
        "next_run_at",
        "last_success_at",
        "failure_count",
        "company",
        "last_complete_at",
        "last_error",
    )
    list_filter = ("kind", "enabled", "adapter", "discovery_pending", "authoritative")
    search_fields = ("name", "url", "notes")
    autocomplete_fields = ("company",)
    readonly_fields = (
        "last_complete_at",
        "lease_token",
        "lease_until",
        "inventory_token",
        "inventory_cursor",
        "inventory_config",
        "inventory_started_at",
        "last_error",
        "last_started_at",
        "last_success_at",
        "failure_count",
        "discovered_from",
        "discovery_url",
    )
    fieldsets = (
        (None, {"fields": ("name", "kind", "company", "url", "adapter", "enabled")}),
        (
            "Review and collection",
            {
                "fields": (
                    "discovery_pending",
                    "review_reason",
                    "employer_name",
                    "authoritative",
                    "closure_enabled",
                    "interval_minutes",
                    "ai_normalization",
                    "notes",
                )
            },
        ),
        (
            "Operational details",
            {
                "classes": ("collapse",),
                "fields": (
                    "max_items",
                    "max_response_bytes",
                    "next_run_at",
                    "next_dispatch_at",
                    "last_started_at",
                    "last_success_at",
                    "last_complete_at",
                    "failure_count",
                    "last_error",
                    "discovery_url",
                    "discovered_from",
                    "lease_token",
                    "lease_until",
                    "inventory_token",
                    "inventory_cursor",
                    "inventory_config",
                    "inventory_started_at",
                ),
            },
        ),
    )
    actions = ["schedule_now"]

    def save_model(self, request, obj, form, change):
        if change and "enabled" in form.changed_data and not obj.enabled:
            obj.review_reason = "Operator disabled automatic activation"
        super().save_model(request, obj, form, change)

    @admin.action(description="Schedule selected sources now", permissions=["change"])
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
        "complete",
        "request_count",
        "duration_seconds",
        "would_close",
    )


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("actor", "application_id", "event", "created_at")


class CompanyAliasInline(admin.TabularInline):
    model = CompanyAlias
    can_delete = False
    extra = 0
    readonly_fields = ("legacy_identity",)


class CareerSourceInline(admin.TabularInline):
    model = Source
    fk_name = "company"
    extra = 0
    show_change_link = True
    can_delete = False
    fields = ("name", "adapter", "url", "enabled", "discovery_pending", "last_complete_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class CompanyProvenanceInline(admin.TabularInline):
    model = CompanyProvenance
    fk_name = "company"
    extra = 0
    can_delete = False
    fields = (
        "provider",
        "reported_name",
        "source_url",
        "reported_website",
        "regional_status",
        "regional_evidence",
        "possible_duplicate",
        "last_observed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "website",
        "website_verified",
        "enriched_at",
        "enrichment_error",
        "review_required",
        "discovery_error",
    )
    list_filter = ("website_verified", "review_required", "discovery_enabled")
    search_fields = ("name", "website", "official_domain", "aliases__name")
    readonly_fields = (
        "identity_key",
        "official_domain",
        "website_verified",
        "discovery_error",
        "logo",
        "logo_source_url",
        "enriched_at",
        "enrichment_attempted_at",
        "enrichment_error",
    )

    inlines = (CompanyAliasInline, CareerSourceInline, CompanyProvenanceInline)
    change_list_template = "admin/jobs/company/change_list.html"
    actions = ("verify_official_websites", "schedule_career_discovery")

    def save_model(self, request, obj, form, change):
        if not obj.identity_key:
            obj.identity_key = new_identity()
        if change and "website" in form.changed_data:
            obj.website_verified = False
            obj.official_domain = None
            obj.review_required = True
            # Previously attested endpoints need review after an identity change.
            obj.career_sources.update(enabled=False, authoritative=False, discovery_pending=True)
        if obj.website:
            obj.website = canonical_url(obj.website)
        # Verification and consolidation use a dedicated action after saving.
        if not change:
            obj.website_verified = False
        super().save_model(request, obj, form, change)

    @admin.action(
        description="Attest selected official websites and consolidate verified domains",
        permissions=["change"],
    )
    def verify_official_websites(self, request, queryset):
        for company in queryset:
            if Company.objects.filter(pk=company.pk).exists():
                try:
                    verify_company(company)
                except ValueError as exc:
                    self.message_user(request, str(exc), level="ERROR")

    @admin.action(description="Schedule career endpoint discovery now", permissions=["change"])
    def schedule_career_discovery(self, request, queryset):
        queryset.filter(website_verified=True).update(next_discovery_at=timezone.now())

    def get_urls(self):
        return [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv),
                name="jobs_company_import",
            )
        ] + super().get_urls()

    def import_csv(self, request):
        from django.core.exceptions import PermissionDenied

        if not self.has_add_permission(request) or not request.user.has_perm("jobs.add_source"):
            raise PermissionDenied
        form = CompanyCSVForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            import io

            upload = form.cleaned_data["file"]
            try:
                created, existing = import_companies(io.StringIO(upload.read().decode("utf-8-sig")))
            except (ValueError, UnicodeError) as exc:
                form.add_error("file", str(exc))
            else:
                self.message_user(
                    request,
                    f"{created} created, {existing} existing. "
                    "Verify official websites before discovery.",
                )
                return HttpResponseRedirect(reverse("admin:jobs_company_changelist"))
        return TemplateResponse(
            request,
            "admin/jobs/company/import_csv.html",
            {
                **self.admin_site.each_context(request),
                "form": form,
                "title": "Import company catalogue",
                "opts": self.model._meta,
            },
        )


class CompanyCSVForm(forms.Form):
    file = forms.FileField()

    def clean_file(self):
        value = self.cleaned_data["file"]
        if value.size > 10 * 1024 * 1024:
            raise ValidationError("CSV exceeds 10 MiB")
        return value


class JobOriginInline(admin.TabularInline):
    model = JobOrigin
    extra = 0
    can_delete = False
    fields = ("source", "source_url", "last_seen_at", "missing_count", "observed_status")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class JobEventInline(admin.TabularInline):
    model = JobEvent
    extra = 0
    can_delete = False
    fields = ("status", "reason", "source", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "company_label",
        "source_labels",
        "location",
        "lifecycle",
        "status_override",
        "active",
        "last_seen_at",
    )
    list_filter = (
        LocationFilter,
        CompanyFilter,
        TechnologyFilter,
        TechnologyScopeFilter,
        "lifecycle",
        "active",
        "verification",
        "status_override",
    )
    search_fields = (
        "title",
        "company",
        "company_profile__name",
        "location",
        "provider_key",
        "canonical_url",
    )
    search_help_text = "Search title, company, location or posting URL"
    show_full_result_count = False
    list_select_related = ("company_profile",)
    autocomplete_fields = ("company_profile", "employer_source")
    readonly_fields = (
        "active",
        "lifecycle",
        "verification",
        "verified_at",
        "imported_content",
        "content_overrides",
        "discovered_at",
        "last_seen_at",
        "status_reason",
        "status_changed_at",
    )
    inlines = (JobOriginInline, JobEventInline)
    actions = ("resume_automatic_status", "resume_imported_content")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("origins__source")

    @admin.display(description="Job source")
    def source_labels(self, obj):
        return ", ".join(origin.source.name for origin in obj.origins.all())

    @admin.display(description="Company", ordering="company")
    def company_label(self, obj):
        return obj.company_profile.name if obj.company_profile_id else obj.company

    def save_model(self, request, obj, form, change):
        from .services.lifecycle import CONTENT_FIELDS

        old = Job.objects.get(pk=obj.pk) if change else None
        overrides = dict(obj.content_overrides)
        for field in CONTENT_FIELDS:
            if field in form.changed_data:
                value = getattr(obj, field)
                overrides[field] = value.isoformat() if field == "posted_at" and value else value
        obj.content_overrides = overrides
        if "apply_email" in form.changed_data:
            obj.email_source = None
        super().save_model(request, obj, form, change)
        if old is None or old.status_override != obj.status_override:
            JobEvent.objects.create(
                job=obj,
                status=obj.status_override or obj.lifecycle,
                reason="admin_override" if obj.status_override else "admin_resume",
            )

    @admin.action(description="Resume automatic lifecycle management", permissions=["change"])
    def resume_automatic_status(self, request, queryset):
        for job in queryset:
            job.status_override = ""
            job.save(update_fields=["status_override"])
            JobEvent.objects.create(job=job, status=job.lifecycle, reason="admin_resume")

    @admin.action(
        description="Remove curated overrides and restore latest imported content",
        permissions=["change"],
    )
    def resume_imported_content(self, request, queryset):
        from .services.lifecycle import CONTENT_FIELDS

        for job in queryset:
            for field, value in job.imported_content.items():
                if field in CONTENT_FIELDS:
                    setattr(job, field, value)
            job.content_overrides = {}
            job.save()


# Register the independent employer-discovery review screens.
from . import admin_discovery  # noqa: E402, F401
