from django.db import migrations

# Frozen defaults: migrations must not depend on changing runtime configuration.
COUNTRIES = (
    "Israel",
    "Ukraine",
    "Austria",
    "Belgium",
    "Bulgaria",
    "Croatia",
    "Cyprus",
    "Czechia",
    "Denmark",
    "Estonia",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Ireland",
    "Italy",
    "Latvia",
    "Lithuania",
    "Luxembourg",
    "Malta",
    "Netherlands",
    "Poland",
    "Portugal",
    "Romania",
    "Slovakia",
    "Slovenia",
    "Spain",
    "Sweden",
)


def seed(apps, schema_editor):
    campaign = apps.get_model("jobs", "EmployerDiscoveryCampaign")
    campaign.objects.using(schema_editor.connection.alias).get_or_create(
        name="Regional technology employers",
        defaults={
            "enabled": False,
            "queries": [
                f'"{country}" ("software engineer" OR "developer" OR "DevOps") careers jobs'
                for country in COUNTRIES
            ],
            "technology_roles": [
                "software engineer",
                "software developer",
                "backend",
                "back-end",
                "frontend",
                "front-end",
                "full stack",
                "full-stack",
                "devops",
                "sre",
                "data engineer",
                "data scientist",
                "machine learning",
                "security engineer",
                "qa engineer",
                "automation engineer",
                "platform engineer",
                "embedded",
                "firmware",
                "systems engineer",
                "python developer",
                "golang developer",
                "rust developer",
            ],
        },
    )


class Migration(migrations.Migration):
    dependencies = [("jobs", "0048_employer_discovery")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
