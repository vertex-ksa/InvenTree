"""Add standalone native approval evidence; no balances or live authority changed."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Persist independent decisions next to additive count sessions."""

    dependencies = [
        ('stock', '0129_cyclecountsession'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='CycleCountApproval',
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
                ('binding', models.JSONField(default=dict)),
                ('expires_at', models.DateTimeField()),
                ('state', models.CharField(default='PENDING', max_length=16)),
                ('revision', models.PositiveIntegerField(default=0)),
                ('commands', models.JSONField(default=dict)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('decision_history', models.JSONField(default=list)),
                (
                    'reviewer',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'session',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='stock.cyclecountsession',
                    ),
                ),
            ],
            options={
                'permissions': [
                    (
                        'approve_cyclecountapproval',
                        'Approve an independent cycle count',
                    ),
                    ('revoke_cyclecountapproval', 'Revoke a cycle count approval'),
                ]
            },
        )
    ]
