"""Native session-authenticated blind-count API role and policy boundaries."""

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse

from rest_framework.test import APIClient

from part.models import Part
from stock.models import CycleCountApproval, StockItem, StockLocation


@override_settings(USE_TZ=True)
class CountApiTests(TestCase):
    """Use native browser sessions and a synthetic explicit server policy."""

    def setUp(self):
        """Create independent native actors, stock and configured authority."""
        self.counter = User.objects.create_user('api-counter')
        self.counter.user_permissions.add(
            *Permission.objects.filter(
                codename__in=[
                    'view_cyclecountsession',
                    'add_cyclecountsession',
                    'change_cyclecountsession',
                ]
            )
        )
        self.reviewer = User.objects.create_user('api-reviewer', is_superuser=True)
        self.other = User.objects.create_user('api-other', is_superuser=True)
        self.location = StockLocation.objects.create(name='Synthetic API location')
        part = Part.objects.create(
            name='Synthetic API part', IPN='SYNTHETIC-1', units='ea'
        )
        self.item = StockItem.objects.create(
            part=part, location=self.location, quantity=10
        )
        self.policy = {
            'tenantId': 'synthetic',
            'environment': 'test',
            'policyVersion': 'api/1',
            'reviewerId': self.reviewer.pk,
            'expirySeconds': 3600,
        }
        self.override = override_settings(COUNT_REVIEW_POLICY=self.policy)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = APIClient()
        self.client.force_login(self.counter)

    def _post(self, route, payload, pk=None):
        return self.client.post(
            reverse(route, kwargs={'pk': pk} if pk else None), payload, format='json'
        )

    def _request(self):
        locations = self.client.get(reverse('api-cycle-count-open'))
        self.assertEqual(locations.status_code, 200)
        self.assertEqual(
            locations.data['locations'],
            [{'id': self.location.pk, 'name': self.location.name}],
        )
        opened = self._post('api-cycle-count-open', {'location_id': self.location.pk})
        self.assertEqual(opened.status_code, 201)
        self.pk = opened.data['sessionId']
        observed = self._post(
            'api-cycle-count-observe',
            {
                'item_id': self.item.pk,
                'quantity': '8.5',
                'command_id': 'observe',
                'expected_revision': 0,
            },
            self.pk,
        )
        self.assertEqual(observed.status_code, 200)
        self.payload = {'command_id': 'request', 'expected_revision': 1}
        receipt = self._post('api-cycle-count-request', self.payload, self.pk)
        self.assertEqual(receipt.status_code, 200)
        return receipt

    def test_disabled_all_routes_and_verbs_before_authentication(self):
        """Disabled routes reveal no authentication or OPTIONS metadata."""
        self.client.logout()
        with override_settings(COUNT_REVIEW_POLICY=None):
            for route in ('open', 'observe', 'request', 'review', 'commit'):
                url = reverse(
                    f'api-cycle-count-{route}',
                    kwargs={'pk': 1} if route != 'open' else None,
                )
                for method in ('get', 'post', 'options', 'put', 'delete'):
                    with self.subTest(route=route, method=method):
                        self.assertEqual(
                            getattr(self.client, method)(url).status_code, 404
                        )

    def test_native_session_blind_review_commit_and_historical_ack(self):
        """The complete native role sequence persists one physical adjustment."""
        self._request()
        self.assertEqual(
            self.client.get(
                reverse('api-stock-detail', kwargs={'pk': self.item.pk})
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse('api-cycle-count-review', kwargs={'pk': self.pk})
            ).status_code,
            403,
        )
        self.assertEqual(
            self._post(
                'api-cycle-count-commit', {'command_id': 'commit'}, self.pk
            ).status_code,
            403,
        )
        self.client.force_login(self.reviewer)
        evidence = self.client.get(
            reverse('api-cycle-count-review', kwargs={'pk': self.pk})
        )
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.data['location']['id'], self.location.pk)
        self.assertEqual(evidence.data['items'][0]['expected'], '10.00000')
        self.assertEqual(evidence.data['items'][0]['delta'], '-1.50000')
        decision = {
            'command_id': 'approve',
            'expected_revision': 0,
            'decision': 'APPROVED',
        }
        self.assertEqual(
            self._post('api-cycle-count-review', decision, self.pk).status_code, 200
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)
        tracking = self.item.tracking_info.count()
        self.assertEqual(
            self._post(
                'api-cycle-count-commit', {'command_id': 'commit'}, self.pk
            ).status_code,
            200,
        )
        self.assertEqual(
            self._post(
                'api-cycle-count-commit', {'command_id': 'commit'}, self.pk
            ).status_code,
            200,
        )
        self.assertEqual(
            self._post('api-cycle-count-review', decision, self.pk).status_code, 200
        )
        self.client.force_login(self.counter)
        ack = self._post('api-cycle-count-request', self.payload, self.pk)
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.data['state'], 'CONSUMED')
        retained = self.client.get(
            reverse('api-cycle-count-observe', kwargs={'pk': self.pk})
        )
        self.assertEqual(retained.data['review']['state'], 'CONSUMED')
        self.assertEqual(
            set(retained.data['review']),
            {'requestId', 'state', 'revision', 'expiresAt'},
        )
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.quantity), '8.50000')
        self.assertEqual(self.item.tracking_info.count(), tracking + 1)

    def test_unknown_authority_fields_and_numeric_quantity_rejected(self):
        """Caller authority fields and decimal coercion never reach domain code."""
        for field in ('tenantId', 'environment', 'hash', 'reviewerId', 'expiresAt'):
            with self.subTest(field=field):
                self.assertEqual(
                    self._post(
                        'api-cycle-count-open',
                        {'location_id': self.location.pk, field: 'forged'},
                    ).status_code,
                    400,
                )
        opened = self._post('api-cycle-count-open', {'location_id': self.location.pk})
        self.assertEqual(
            self._post(
                'api-cycle-count-observe',
                {
                    'item_id': self.item.pk,
                    'quantity': 8.5,
                    'command_id': 'bad',
                    'expected_revision': 0,
                },
                opened.data['sessionId'],
            ).status_code,
            400,
        )
        self.assertEqual(
            self._post('api-cycle-count-open', {'location_id': True}).status_code, 400
        )

    def test_config_change_invalidates_request_ack_review_and_consume(self):
        """Every configured authority field participates in the exact binding."""
        self._request()
        self.client.force_login(self.reviewer)
        self.assertEqual(
            self._post(
                'api-cycle-count-review',
                {
                    'command_id': 'approve',
                    'expected_revision': 0,
                    'decision': 'APPROVED',
                },
                self.pk,
            ).status_code,
            200,
        )
        for key, value in {
            'reviewerId': self.other.pk,
            'expirySeconds': 3601,
            'environment': 'changed',
            'policyVersion': 'api/2',
            'tenantId': 'changed',
        }.items():
            with (
                self.subTest(key=key),
                override_settings(COUNT_REVIEW_POLICY={**self.policy, key: value}),
            ):
                self.assertEqual(
                    self._post(
                        'api-cycle-count-commit', {'command_id': 'commit'}, self.pk
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    self.client.get(
                        reverse('api-cycle-count-review', kwargs={'pk': self.pk})
                    ).status_code,
                    409,
                )
                self.client.force_login(self.counter)
                self.assertEqual(
                    self._post(
                        'api-cycle-count-request', self.payload, self.pk
                    ).status_code,
                    409,
                )
                self.client.force_login(self.reviewer)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 10)
        self.assertEqual(
            CycleCountApproval.objects.get(session_id=self.pk).state, 'APPROVED'
        )

    def test_invalid_server_policy_and_anonymous_access_fail_closed(self):
        """Malformed installation policy never substitutes a default authority."""
        for config in (
            {**self.policy, 'expirySeconds': True},
            {**self.policy, 'expirySeconds': 1.5},
            {**self.policy, 'extra': 'bad'},
            {**self.policy, 'environment': ' '},
        ):
            with (
                self.subTest(config=config),
                override_settings(COUNT_REVIEW_POLICY=config),
            ):
                self.assertEqual(
                    self._post(
                        'api-cycle-count-open', {'location_id': self.location.pk}
                    ).status_code,
                    503,
                )
        self.client.logout()
        self.assertEqual(
            self._post(
                'api-cycle-count-open', {'location_id': self.location.pk}
            ).status_code,
            403,
        )

    def test_native_session_requires_csrf_for_mutation(self):
        """A real native session cannot post a command without CSRF protection."""
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.counter)
        self.assertEqual(
            client.post(
                reverse('api-cycle-count-open'),
                {'location_id': self.location.pk},
                format='json',
            ).status_code,
            403,
        )
