# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-08-04 20:57
from __future__ import unicode_literals

from django.db import migrations
from temba.utils import chunk_list
from django.db.models import F


def backfill_urn_identity(apps, schema_editor):
    ContactURN = apps.get_model('contacts', 'ContactURN')

    urns = ContactURN.objects.filter(identity=None).values_list('id', flat=True)
    count = 0

    if urns:
        print("found %d urns to backfill" % len(urns))

    for batch in chunk_list(urns, 1000):
        ContactURN.objects.filter(id__in=batch).update(identity=F('urn'))
        count += len(batch)
        print("backfilled %d of %d URNs" % (count, len(urns)))


class Migration(migrations.Migration):

    dependencies = [
        ('contacts', '0064_auto_20170804_1918'),
    ]

    operations = [
        migrations.RunPython(backfill_urn_identity),
    ]
