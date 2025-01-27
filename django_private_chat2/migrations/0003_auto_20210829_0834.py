# Generated by Django 3.2.6 on 2021-08-29 08:34

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('django_private_chat2', '0002_auto_20210329_2217'),
    ]

    operations = [
        migrations.AddField(
            model_name='messagemodel',
            name='pid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='public id'),
        ),
        migrations.AddIndex(
            model_name='messagemodel',
            index=models.Index(fields=['pid'], name='private_chat_message_pid_idx'),
        ),
    ]
