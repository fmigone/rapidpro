# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-10-13 19:24
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('flows', '0112_fix_sql_func'),
    ]

    operations = [
        migrations.AddField(
            model_name='flowrun',
            name='results',
            field=models.TextField(help_text='The results collected during this flow run in JSON format', null=True),
        ),
    ]
