"""Bounded admin filters: text inputs avoid listing thousands of locations/companies."""

from django.contrib import admin
from django.db.models import Q

from .services.normalization import TECHNOLOGIES


class TextFilter(admin.SimpleListFilter):
    template = "admin/jobs/text_filter.html"

    def lookups(self, request, model_admin):
        return ()

    def has_output(self):
        return True


class LocationFilter(TextFilter):
    title = "Location contains"
    parameter_name = "location"

    def queryset(self, request, queryset):
        value = (self.value() or "").strip()[:250]
        return queryset.filter(location__icontains=value) if value else queryset


class CompanyFilter(TextFilter):
    title = "Company contains"
    parameter_name = "company"

    def queryset(self, request, queryset):
        value = (self.value() or "").strip()[:250]
        if value:
            return queryset.filter(
                Q(company__icontains=value) | Q(company_profile__name__icontains=value)
            )
        return queryset


class TechnologyFilter(admin.SimpleListFilter):
    title = "Technology"
    parameter_name = "technology"

    def lookups(self, request, model_admin):
        return [(name, name) for name in TECHNOLOGIES]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        if value not in TECHNOLOGIES:
            return queryset.none()
        field = (
            "required_technologies"
            if request.GET.get("technology_scope") == "required"
            else "technologies"
        )
        return queryset.filter(normalized__contains={field: [value]})


class TechnologyScopeFilter(admin.SimpleListFilter):
    title = "Technology match"
    parameter_name = "technology_scope"

    def lookups(self, request, model_admin):
        return [("mentioned", "Mentioned anywhere"), ("required", "Required qualifications only")]

    def queryset(self, request, queryset):
        # TechnologyFilter applies the selected scope; this input alone does not exclude jobs.
        return queryset
