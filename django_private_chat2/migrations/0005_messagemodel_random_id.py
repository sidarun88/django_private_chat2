# Generated by Django 3.2.6 on 2021-09-05 09:56

from django.db import migrations, models
import django_private_chat2.models


class Migration(migrations.Migration):

    dependencies = [
        ('django_private_chat2', '0004_auto_20210905_0817'),
    ]

    operations = [
        migrations.AddField(
            model_name='messagemodel',
            name='random_id',
            field=models.BigIntegerField(default=django_private_chat2.models.generate_random_number, verbose_name='random id'),
        ),
    ]
