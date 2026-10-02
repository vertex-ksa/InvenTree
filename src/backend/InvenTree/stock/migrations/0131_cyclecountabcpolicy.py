"""Persist independent ABC proposal governance without stock authority."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add immutable policy candidates and explicit native approval grants."""

    dependencies = [
        ('stock', '0130_cyclecountapproval'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CycleCountABCPolicy',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('request_key', models.CharField(max_length=64, unique=True)),
                ('proposal_hash', models.CharField(max_length=64)),
                ('binding', models.JSONField(default=dict)),
                ('policy', models.JSONField(default=dict)),
                ('annual_usage_values', models.JSONField(default=dict)),
                ('source_reference', models.CharField(max_length=255)),
                (
                    'source_qualification',
                    models.CharField(
                        default='OPERATOR_INPUT_NOT_NATIVE_INGESTION', max_length=64
                    ),
                ),
                ('state', models.CharField(default='PENDING', max_length=16)),
                ('revision', models.PositiveIntegerField(default=0)),
                ('commands', models.JSONField(default=dict)),
                ('decision_history', models.JSONField(default=list)),
                ('created', models.DateTimeField(auto_now_add=True)),
                (
                    'location',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='stock.stocklocation',
                    ),
                ),
                (
                    'requester',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'reviewer',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'permissions': [
                    (
                        'approve_cyclecountabcpolicy',
                        'Approve an independent ABC count policy',
                    )
                ]
            },
        )
    ]
