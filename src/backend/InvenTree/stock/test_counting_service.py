"""Native persisted approval and count cases; callback fakes test domain isolation."""

from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from rest_framework.test import APIClient

from common.settings import set_global_setting
from part.models import Part
from stock.counting import CountConflictError
from stock.counting_approval import (
    commit_native_count,
    decide_native_review,
    request_native_review,
)
from stock.counting_records import observer_record
from stock.counting_service import (
    commit_count,
    observe_count,
    open_count,
    request_count_review,
)
from stock.models import CycleCountApproval, CycleCountSession, StockItem, StockLocation
from users.models import Owner


@override_settings(USE_TZ=True)
class NativeCountTests(TestCase):
    """Use production timezone semantics for explicit aware approval expiry.

    InvenTree intentionally disables USE_TZ in its general test settings.
    """

    def setUp(self):
        """Use isolated synthetic people and stock."""
        self.counter = User.objects.create_user('counter')
        self.counter.user_permissions.add(
            *Permission.objects.filter(
                codename__in=[
                    'view_cyclecountsession',
                    'add_cyclecountsession',
                    'change_cyclecountsession',
                ]
            )
        )
        self.reviewer = User.objects.create_user('reviewer', is_superuser=True)
        self.location = StockLocation.objects.create(name='Synthetic count location')
        self.part = Part.objects.create(name='Synthetic count part')
        self.item = StockItem.objects.create(
            part=self.part, location=self.location, quantity=10
        )
        self.session = open_count(self.location, self.counter)

    def _observe(self):
        return observe_count(
            self.session.pk, self.counter, self.item.pk, '8.5', 'observe-1', 0
        )

    def _review(self):
        session = self._observe()
        return request_count_review(
            session.pk,
            self.counter,
            1,
            'synthetic',
            'test',
            'count/1',
            lambda binding, actor: 'fake-request',
        )

    def _native_review(self):
        self._observe()
        self.expiry = timezone.now() + timedelta(hours=1)
        return request_native_review(
            self.session.pk,
            self.counter,
            1,
            'synthetic',
            'test',
            'count/1',
            self.reviewer,
            self.expiry,
            'request-1',
        )

    def _native_decide(self, decision='APPROVED', revision=0, command='decision-1'):
        return decide_native_review(
            self.session.pk,
            self.reviewer,
            revision,
            command,
            decision,
            'synthetic',
            'test',
            'count/1',
            reason='Synthetic decision' if decision in ('REJECTED', 'REVOKED') else '',
        )

    def _native_commit(self, command='native-commit-1'):
        return commit_native_count(
            self.session.pk, self.reviewer, command, 'synthetic', 'test', 'count/1'
        )

    def test_persisted_native_approval_and_commit_replay(self):
        """Approval changes no balance; consumption performs one native adjustment."""
        request = self._native_review()
        replay = request_native_review(
            self.session.pk,
            self.counter,
            1,
            'synthetic',
            'test',
            'count/1',
            self.reviewer,
            self.expiry,
            'request-1',
        )
        self.assertEqual(request, replay)
        decision = self._native_decide()
        self.assertEqual(decision, self._native_decide())
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)
        before = self.item.tracking_info.count()
        receipt = self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.quantity), '8.50000')
        history = self.item.tracking_info.count()
        self.assertEqual(history, before + 1)
        self.assertEqual(receipt, self._native_commit())
        self.assertEqual(history, self.item.tracking_info.count())
        self.assertEqual(self._native_decide()['state'], 'CONSUMED')
        acknowledged = request_native_review(
            self.session.pk,
            self.counter,
            1,
            'synthetic',
            'test',
            'count/1',
            self.reviewer,
            self.expiry,
            'request-1',
        )
        self.assertEqual(acknowledged['state'], 'CONSUMED')
        self.assertEqual(history, self.item.tracking_info.count())
        self.assertEqual(
            CycleCountApproval.objects.get(session=self.session).state, 'CONSUMED'
        )
        with self.assertRaises(CountConflictError):
            self._native_commit('different-command')

    def test_native_approval_revocation(self):
        """A revoked request cannot be consumed or overwritten."""
        self._native_review()
        self._native_decide()
        self._native_decide('REVOKED', 1, 'revoke-1')
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_native_approval_expiry(self):
        """An approved request expires independently of its decision state."""
        self._native_review()
        self._native_decide()
        approval = CycleCountApproval.objects.get(session=self.session)
        approval.expires_at = timezone.now() - timedelta(seconds=1)
        approval.save(update_fields=['expires_at'])
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_native_approval_reviewer_removed(self):
        """Removing assigned reviewer makes a persisted approval unusable."""
        self._native_review()
        self._native_decide()
        self.reviewer.delete()
        controller = User.objects.create_user('controller', is_superuser=True)
        with self.assertRaises(CountConflictError):
            commit_native_count(
                self.session.pk, controller, 'commit', 'synthetic', 'test', 'count/1'
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_native_approval_current_reviewer_grant_required(self):
        """Decision-time permission does not survive removal before consumption."""
        self.reviewer.is_superuser = False
        self.reviewer.save()
        self.reviewer.user_permissions.add(
            *Permission.objects.filter(
                codename__in=[
                    'view_stockitem',
                    'change_stockitem',
                    'approve_cyclecountapproval',
                ]
            )
        )
        self._native_review()
        self._native_decide()
        self.reviewer.user_permissions.remove(
            Permission.objects.get(codename='approve_cyclecountapproval')
        )
        with self.assertRaises(PermissionDenied):
            self._native_commit()

    def test_native_approval_material_change_and_command_conflicts(self):
        """CAS and immutable payload checks protect decision and stock commit."""
        self._native_review()
        with self.assertRaises(CountConflictError):
            self._native_decide(revision=1)
        self._native_decide()
        with self.assertRaises(CountConflictError):
            self._native_decide('REJECTED')
        self.item.stocktake('9', self.reviewer, notes='Synthetic intervening movement')
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.assertEqual(
            CycleCountApproval.objects.get(session=self.session).state, 'APPROVED'
        )

    def test_native_approval_requester_cannot_decide(self):
        """Even a stock superuser cannot approve their own evidence."""
        self._native_review()
        self.counter.is_superuser = True
        self.counter.save()
        with self.assertRaises(PermissionDenied):
            decide_native_review(
                self.session.pk,
                self.counter,
                0,
                'self',
                'APPROVED',
                'synthetic',
                'test',
                'count/1',
            )

    def test_native_approval_state_without_decision_is_not_authority(self):
        """A mutable state flag cannot replace a bound persisted decision."""
        self._native_review()
        CycleCountApproval.objects.filter(session=self.session).update(state='APPROVED')
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_native_approval_normalizes_non_utc_expiry(self):
        """A trusted non-UTC aware expiry survives database reload and replay."""
        from datetime import timezone as datetime_timezone

        self._observe()
        expiry = (timezone.now() + timedelta(hours=1)).astimezone(
            datetime_timezone(timedelta(hours=3))
        )
        arguments = (
            self.session.pk,
            self.counter,
            1,
            'synthetic',
            'test',
            'count/1',
            self.reviewer,
            expiry,
            'request-timezone',
        )
        receipt = request_native_review(*arguments)
        self.assertEqual(receipt, request_native_review(*arguments))
        self._native_decide()
        self._native_commit()

    def test_native_adjustment_and_replay(self):
        """Commit through native stocktake and preserve one tracking effect."""
        session = self._review()
        validator = lambda request, binding, actor, action: True
        committed = commit_count(
            session.pk,
            self.reviewer,
            'commit-1',
            'synthetic',
            'test',
            'count/1',
            validator,
        )
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.quantity), '8.50000')
        history = self.item.tracking_info.count()
        replay = commit_count(
            session.pk,
            self.reviewer,
            'commit-1',
            'synthetic',
            'test',
            'count/1',
            validator,
        )
        self.assertEqual(committed['sessionId'], replay['sessionId'])
        self.assertEqual(set(replay), {'sessionId', 'state', 'commandId'})
        self.assertEqual(self.item.tracking_info.count(), history)
        with self.assertRaises(CountConflictError):
            commit_count(
                session.pk,
                self.reviewer,
                'commit-1',
                'synthetic',
                'live',
                'count/1',
                validator,
            )

    def test_same_command_different_payload_conflicts(self):
        """An operation key cannot hide changed physical observations."""
        self._observe()
        with self.assertRaises(CountConflictError):
            observe_count(
                self.session.pk, self.counter, self.item.pk, '9', 'observe-1', 1
            )

    def test_no_self_approval_or_missing_producer(self):
        """Fail closed without an independent authorized approval."""
        session = self._review()
        with self.assertRaises(PermissionDenied):
            commit_count(
                session.pk,
                self.counter,
                'commit-1',
                'synthetic',
                'test',
                'count/1',
                lambda *args: True,
            )
        with self.assertRaises(CountConflictError):
            commit_count(
                session.pk, self.reviewer, 'commit-1', 'synthetic', 'test', 'count/1'
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_intervening_native_movement_blocks(self):
        """A movement after scope freeze requires a fresh count."""
        session = self._review()
        self.item.stocktake('11', self.reviewer)
        with self.assertRaises(CountConflictError):
            commit_count(
                session.pk,
                self.reviewer,
                'commit-1',
                'synthetic',
                'test',
                'count/1',
                lambda *args: True,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 11)

    def test_stale_observations_do_not_create_review_request(self):
        """Surface movement conflicts before requesting independent approval."""
        self._observe()
        self.item.stocktake('11', self.reviewer)
        with self.assertRaises(CountConflictError):
            request_count_review(
                self.session.pk,
                self.counter,
                1,
                'synthetic',
                'test',
                'count/1',
                lambda *args: self.fail('Stale evidence sent to producer'),
            )

    def test_revoked_counter_denied(self):
        """Recheck active identity even with an existing in-memory actor."""
        self.counter.is_active = False
        self.counter.save()
        with self.assertRaises(PermissionDenied):
            self._observe()

    def test_location_ownership_denied_despite_stock_permission(self):
        """A native model grant cannot bypass location ownership control."""
        self.counter.is_superuser = False
        self.counter.save()
        self.counter.user_permissions.add(
            Permission.objects.get(codename='change_stockitem')
        )
        set_global_setting('STOCK_OWNERSHIP_CONTROL', True)
        self.addCleanup(set_global_setting, 'STOCK_OWNERSHIP_CONTROL', False)
        self.location.owner = Owner.get_owner(self.reviewer)
        self.location.save()
        with self.assertRaises(PermissionDenied):
            self._observe()

    def test_counter_cannot_bypass_review_via_native_count_api(self):
        """Observation grants do not grant the legacy stock mutation endpoint."""
        client = APIClient()
        client.force_authenticate(user=self.counter)
        response = client.post(
            reverse('api-stock-count'),
            {'items': [{'pk': self.item.pk, 'quantity': '1'}]},
            format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_blind_counter_can_count_without_native_stock_read(self):
        """Dedicated observations work while ordinary stock balances are denied."""
        self.assertFalse(self.counter.has_perm('stock.view_stockitem'))
        self._observe()
        client = APIClient()
        client.force_authenticate(user=self.counter)
        response = client.get(reverse('api-stock-detail', kwargs={'pk': self.item.pk}))
        self.assertEqual(response.status_code, 403)
        record = observer_record(self.session.pk, self.counter)
        self.assertEqual(record['items'][0]['observed'], '8.50000')
        self.assertEqual(
            set(record), {'sessionId', 'state', 'revision', 'location', 'items'}
        )
        self.assertEqual(
            set(record['items'][0]),
            {
                'id',
                'partId',
                'partName',
                'partIPN',
                'partRevision',
                'unit',
                'batch',
                'observed',
            },
        )
        with self.assertRaises(PermissionDenied):
            observer_record(self.session.pk, self.reviewer)

    def test_native_approval_part_change_requires_recount(self):
        """A stock row reassigned to another SKU cannot consume old observations."""
        self._native_review()
        self._native_decide()
        self.item.part = Part.objects.create(name='Different synthetic SKU')
        self.item.save(add_note=False)
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)
        self.assertEqual(
            CycleCountApproval.objects.get(session=self.session).state, 'APPROVED'
        )

    def test_native_part_identifier_edits_require_recount(self):
        """Native SKU labels and revision changes invalidate frozen identity."""
        self._native_review()
        self._native_decide()
        for field in ('IPN', 'revision', 'name'):
            with self.subTest(field=field):
                original = getattr(self.part, field)
                setattr(self.part, field, 'Changed synthetic identifier')
                self.part.save(update_fields=[field])
                with self.assertRaises(CountConflictError):
                    self._native_commit()
                self.item.refresh_from_db()
                self.assertEqual(self.item.quantity, 10)
                setattr(self.part, field, original)
                self.part.save(update_fields=[field])

    def test_native_approval_unit_change_requires_recount(self):
        """Independent part-unit edits are material even without stock history."""
        self._native_review()
        self._native_decide()
        self.part.units = 'kg'
        self.part.save(update_fields=['units'])
        with self.assertRaises(CountConflictError):
            self._native_commit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)

    def test_native_approval_batch_change_requires_recount(self):
        """A different physical batch cannot reuse an old frozen count."""
        self._native_review()
        self._native_decide()
        self.item.batch = 'Different synthetic batch'
        self.item.save(add_note=False)
        with self.assertRaises(CountConflictError):
            self._native_commit()

    def test_incomplete_legacy_material_scope_requires_recount(self):
        """Incomplete old draft evidence is retained rather than reinterpreted."""
        self._native_review()
        session = CycleCountSession.objects.get(pk=self.session.pk)
        session.scope[0].pop('unit')
        session.save(update_fields=['scope'])
        with self.assertRaises(CountConflictError):
            self._native_decide()
        with self.assertRaises(CountConflictError):
            observer_record(session.pk, self.counter)

    def test_observation_replay_rechecks_item_ownership(self):
        """A lost acknowledgement cannot expose evidence after ownership removal."""
        self._observe()
        self.counter.is_superuser = False
        self.counter.save()
        self.counter.user_permissions.add(
            Permission.objects.get(codename='change_stockitem')
        )
        set_global_setting('STOCK_OWNERSHIP_CONTROL', True)
        self.addCleanup(set_global_setting, 'STOCK_OWNERSHIP_CONTROL', False)
        self.item.owner = Owner.get_owner(self.reviewer)
        self.item.save()
        with self.assertRaises(PermissionDenied):
            self._observe()
