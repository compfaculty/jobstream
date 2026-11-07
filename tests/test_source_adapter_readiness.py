import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory

from jobs.admin import SourceAdmin
from jobs.models import Source


def pending_source(**changes):
    return Source(
        name="Pending careers",
        kind=Source.Kind.COMPANY,
        url="https://example.com/careers",
        needs_adapter=True,
        **changes,
    )


def test_pending_source_can_have_no_adapter():
    pending_source().clean()


@pytest.mark.parametrize("field", ["enabled", "authoritative", "closure_enabled"])
def test_pending_source_cannot_enable_collection_or_authority(field):
    with pytest.raises(ValidationError, match="awaiting an adapter"):
        pending_source(**{field: True}).clean()


def test_adapter_required_when_backlog_flag_cleared():
    source = pending_source()
    source.needs_adapter = False
    with pytest.raises(ValidationError, match="Select an adapter"):
        source.clean()
    source.adapter = Source.Adapter.JSONLD
    source.clean()


def test_admin_exposes_backlog_and_optional_adapter():
    model_admin = SourceAdmin(Source, admin.site)
    request = RequestFactory().get("/admin/jobs/source/add/")
    request.user = AnonymousUser()
    form = model_admin.get_form(request)
    assert "needs_adapter" in form.base_fields
    assert not form.base_fields["adapter"].required
    assert "needs_adapter" in model_admin.list_filter
    assert "needs_adapter" in model_admin.list_display


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["enabled", "authoritative", "closure_enabled"])
def test_database_rejects_pending_source_activation(field):
    source = pending_source()
    source.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        Source.objects.filter(pk=source.pk).update(**{field: True})


@pytest.mark.django_db
def test_database_requires_adapter_after_backlog_flag_cleared():
    source = pending_source()
    source.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        Source.objects.filter(pk=source.pk).update(needs_adapter=False)
    source.adapter = Source.Adapter.JSONLD
    source.needs_adapter = False
    source.save()


@pytest.mark.django_db
def test_admin_saves_pending_source_without_placeholder_adapter():
    model_admin = SourceAdmin(Source, admin.site)
    request = RequestFactory().get("/admin/jobs/source/add/")
    request.user = AnonymousUser()
    form_class = model_admin.get_form(request)
    form = form_class(
        data={
            "name": "Unsupported board",
            "kind": Source.Kind.BOARD,
            "url": "https://example.com/careers",
            "needs_adapter": "on",
            "adapter": "",
            "interval_minutes": "1440",
            "max_items": "100",
            "max_response_bytes": "2097152",
            "next_run_at_0": "2026-11-08",
            "next_run_at_1": "00:00:00",
            "next_dispatch_at_0": "2026-11-08",
            "next_dispatch_at_1": "00:00:00",
        }
    )
    assert form.is_valid(), form.errors
    source = form.save()
    assert source.needs_adapter
    assert not source.enabled
    assert source.adapter == ""
