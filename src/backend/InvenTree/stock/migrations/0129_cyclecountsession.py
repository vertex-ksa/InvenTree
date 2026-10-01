"""Add opt-in count evidence without altering existing stock balances."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Compatible additive schema; no stock backfill or quantity writes."""

    dependencies = [
        ('stock', '0128_remove_stockitem_notes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='CycleCountSession',
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
                ('scope', models.JSONField(default=list)),
                ('observations', models.JSONField(default=dict)),
                ('commands', models.JSONField(default=dict)),
                ('revision', models.PositiveIntegerField(default=0)),
                ('state', models.CharField(default='OPEN', max_length=16)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('approval_request', models.CharField(blank=True, max_length=128)),
                ('committed_command', models.CharField(blank=True, max_length=128)),
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
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={'verbose_name': 'Cycle Count Session'},
        )
    ]
