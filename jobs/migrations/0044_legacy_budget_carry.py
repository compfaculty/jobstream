"""Carry legacy token commitments conservatively; resetting must not replenish spend."""

from decimal import Decimal

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def carry(apps, schema_editor):
    Month = apps.get_model("jobs", "AICostMonth")
    Quota = apps.get_model("jobs", "AIQuota")
    month = timezone.now().date().replace(day=1)
    if Month.objects.filter(month=month).exists():
        return
    # Historical records cannot identify all models/outcomes. Use the highest configured
    # uncached rate for every committed token, not an invented precise invoice amount.
    rate = max(Decimal(str(v)) for pair in settings.AI_MODEL_PRICES.values() for v in pair)
    tokens = sum(Quota.objects.filter(day__gte=month).values_list("reserved_tokens", flat=True))
    value = (Decimal(tokens) * rate / Decimal(1000000)).quantize(Decimal("0.00000001"))
    Month.objects.create(month=month, committed_usd=value, carried_usd=value)


class Migration(migrations.Migration):
    dependencies = [("jobs", "0043_ai_cache_reuse")]
    operations = [migrations.RunPython(carry, migrations.RunPython.noop)]
