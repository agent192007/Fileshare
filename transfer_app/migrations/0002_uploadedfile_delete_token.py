from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("File", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="uploadedfile",
            name="delete_token",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
