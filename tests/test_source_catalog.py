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
    assert all(source.max_items > 0 for source in catalog)


def test_reference_catalogue_is_not_automatically_seeded(settings):
    settings.DEBUG = True
    call_command("bootstrap_development", stdout=StringIO())
    assert Source.objects.count() == 7
    assert not Source.objects.filter(enabled=True).exists()
