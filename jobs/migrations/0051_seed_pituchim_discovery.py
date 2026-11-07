from django.db import migrations


# Keep the no-key directory campaign separate from the saved Brave configuration.
def seed(apps, schema_editor):
    campaign = apps.get_model("jobs", "EmployerDiscoveryCampaign")
    campaign.objects.using(schema_editor.connection.alias).get_or_create(
        name="Pituchim hiring companies",
        defaults={
            "provider": "pituchim",
            "enabled": False,
            "queries": [],
            "daily_search_limit": 30,
            "daily_candidate_limit": 100,
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
    dependencies = [("jobs", "0050_pituchim_directory")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
