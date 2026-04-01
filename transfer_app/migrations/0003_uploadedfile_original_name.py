from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("File", "0002_uploadedfile_delete_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="uploadedfile",
            name="original_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
