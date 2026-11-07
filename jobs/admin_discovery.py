"""Staff-only discovery operations; ordinary model permissions remain mandatory."""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import EmployerDiscoveryCampaign, EmployerDiscoveryLead, EmployerDiscoveryRun
from .services.employer_discovery import promote, start


@admin.register(EmployerDiscoveryCampaign)
class EmployerDiscoveryCampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "next_run_at", "daily_search_limit", "daily_candidate_limit")
    list_filter = ("enabled",)
    readonly_fields = ("query_cursor", "query_pages")
    actions = ("run_now",)

    @admin.action(
        description="Request a bounded run (enabled campaigns only)", permissions=["change"]
    )
    def run_now(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        for campaign in queryset[:100]:
            run = start(campaign.pk, manual=True)
            if run:
                self.log_change(request, campaign, "Requested employer discovery run")
        self.message_user(request, "Enabled campaigns queued; daily limits still apply.")


@admin.register(EmployerDiscoveryRun)
class EmployerDiscoveryRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "campaign",
        "created_at",
        "status",
        "searched",
        "inspected",
        "confirmed",
        "unresolved",
        "duplicate",
        "rejected",
        "failed",
        "diagnostic",
    )
    list_filter = ("status", "campaign")
    readonly_fields = tuple(field.name for field in EmployerDiscoveryRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployerDiscoveryLead)
class EmployerDiscoveryLeadAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "candidate_url",
        "status",
        "vacancy_title",
        "vacancy_location",
        "observed_at",
        "diagnostic",
        "company",
    )
    list_filter = ("status", "campaign", "evidence_kind")
    search_fields = ("name", "website", "candidate_url", "vacancy_title", "vacancy_location")
    readonly_fields = (
        "campaign",
        "run",
        "key",
        "candidate_url",
        "search_query",
        "search_offset",
        "status",
        "diagnostic",
        "company",
        "created_at",
    )
    actions = ("promote_selected", "reject_selected")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_promote_permission(self, request):
        return self.has_change_permission(request) and request.user.has_perms(
            ["jobs.add_company", "jobs.add_source"]
        )

    @admin.action(
        description="Promote leads with recorded vacancy/relationship evidence",
        permissions=["promote"],
    )
    def promote_selected(self, request, queryset):
        if not self.has_promote_permission(request):
            raise PermissionDenied
        promoted = refused = 0
        for lead in queryset[:100]:
            try:
                promote(lead.pk)
            except ValidationError:
                refused += 1
            else:
                promoted += 1
                self.log_change(request, lead, "Promoted recorded employer discovery evidence")
        self.message_user(
            request,
            f"Promoted: {promoted}; evidence requires review: {refused}.",
            messages.WARNING if refused else messages.SUCCESS,
        )

    @admin.action(description="Reject selected leads", permissions=["change"])
    def reject_selected(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        with transaction.atomic():
            for lead in queryset.select_for_update().order_by("pk")[:100]:
                lead.status = "rejected"
                lead.diagnostic = "OperatorRejected"
                lead.save(update_fields=["status", "diagnostic"])
                self.log_change(request, lead, "Rejected employer discovery lead")
        self.message_user(request, "Leads rejected. Existing company records are preserved.")

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            current = EmployerDiscoveryLead.objects.select_for_update().get(pk=obj.pk)
            # A stale edit form must not undo a concurrent operator rejection.
            obj.status = "rejected" if current.status == "rejected" else "unresolved"
            obj.company_id = current.company_id
            super().save_model(request, obj, form, change)
