from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("jobs", "0035_preference_profile")]
    operations = [
        migrations.AddField(
            model_name="application",
            name="assisted_delivery",
            field=models.BooleanField(default=False),
        ),
    ]
