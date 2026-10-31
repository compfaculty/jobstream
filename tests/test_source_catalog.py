from io import StringIO

import pytest
from django.core.management import call_command

from jobs.models import Source
from jobs.services.network import canonical_url
from jobs.services.sources import ADAPTERS
from jobs.source_catalog import definitions

pytestmark = pytest.mark.django_db


def test_catalog_is_unique_and_supported():
    catalog = definitions()
    secret = next(source for source in catalog if source.adapter == "secret_telaviv")
    assert secret.url == "https://jobs.secrettelaviv.com/list/category/programmer/"
    assert len(catalog) == 54
    assert len({source.name for source in catalog}) == len(catalog)
    assert len({canonical_url(source.url) for source in catalog}) == len(catalog)
    assert {source.adapter for source in catalog} <= ADAPTERS.keys()
    for source in catalog:
        instance = Source(
            name=source.name,
            adapter=source.adapter,
            url=source.url,
            notes=source.notes,
            max_items=source.max_items,
            employer_name=source.employer_name,
            authoritative=source.authoritative,
            max_response_bytes=source.max_response_bytes,
        )
        instance.full_clean(validate_unique=False)


def test_seed_idempotent_preserves_operator_settings():
    original = Source.objects.create(
        name="My renamed DOU source",
        adapter="rss",
        url="https://jobs.dou.ua/vacancies/feeds/?category=Python",
        enabled=True,
        interval_minutes=30,
        notes="Operator notes",
    )
    board = Source.objects.create(
        name="Secret Tel Aviv",
        adapter="secret_telaviv",
        url="https://custom.example.com/careers",
        enabled=True,
        interval_minutes=90,
    )
    call_command("seed_sources", stdout=StringIO())
    first_count = Source.objects.count()
    call_command("seed_sources", stdout=StringIO())
    assert Source.objects.count() == first_count == 54
    original.refresh_from_db()
    board.refresh_from_db()
    assert original.name == "My renamed DOU source"
    assert (
        original.enabled and original.interval_minutes == 30 and original.notes == "Operator notes"
    )
    assert board.enabled and board.url == "https://custom.example.com/careers"
    assert not Source.objects.exclude(pk__in=[original.pk, board.pk]).filter(enabled=True).exists()


def test_seed_dry_run_has_no_writes():
    output = StringIO()
    call_command("seed_sources", dry_run=True, stdout=output)
    assert not Source.objects.exists()
    assert "Planned 54" in output.getvalue()
