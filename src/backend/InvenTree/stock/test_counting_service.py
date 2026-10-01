"""Native count adapter regression cases; fake approval is NOT_INTEGRATED."""

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from part.models import Part
from stock.counting import CountConflictError
from stock.counting_service import (
    commit_count,
    observe_count,
    open_count,
    request_count_review,
)
from stock.models import StockItem, StockLocation


class NativeCountTests(TestCase):
    """Ensure native tracking, replay and authorization protect physical stock."""

    def setUp(self):
        """Use isolated synthetic people and stock."""
        self.counter = User.objects.create_user('counter', is_superuser=True)
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
        self.assertEqual(committed.pk, replay.pk)
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

    def test_revoked_counter_denied(self):
        """Recheck active identity even with an existing in-memory actor."""
        self.counter.is_active = False
        self.counter.save()
        with self.assertRaises(PermissionDenied):
            self._observe()
