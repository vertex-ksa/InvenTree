"""Native count adapter regression cases; fake approval is NOT_INTEGRATED."""

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from rest_framework.test import APIClient

from common.settings import set_global_setting
from part.models import Part
from stock.counting import CountConflictError
from stock.counting_service import (
    commit_count,
    observe_count,
    open_count,
    request_count_review,
)
from stock.models import StockItem, StockLocation
from users.models import Owner


class NativeCountTests(TestCase):
    """Ensure native tracking, replay and authorization protect physical stock."""

    def setUp(self):
        """Use isolated synthetic people and stock."""
        self.counter = User.objects.create_user('counter')
        self.counter.user_permissions.add(
            *Permission.objects.filter(
                codename__in=[
                    'view_stockitem',
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
