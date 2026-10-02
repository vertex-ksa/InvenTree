"""Native independent ABC planning, CAS and immutable material evidence."""

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse

from rest_framework.test import APIClient

from part.models import Part
from stock.counting import CountConflictError
from stock.counting_abc_governance import (
    abc_policy_context,
    approved_abc_schedule,
    decide_abc_policy,
    lookup_abc_decision,
    lookup_abc_proposal,
    propose_abc_policy,
    read_abc_policy,
)
from stock.counting_api import installation_policy
from stock.models import (
    CycleCountABCPolicy,
    StockItem,
    StockItemTracking,
    StockLocation,
)


class ABCPolicyGovernanceTests(TestCase):
    """Use distinct explicitly permissioned native economic actors."""

    def setUp(self):
        """Configure manual proposal authority without stock or live posting effects."""
        self.planner = User.objects.create_user('abc-planner')
        self.reviewer = User.objects.create_user('abc-reviewer')
        self.other = User.objects.create_user('abc-other-reviewer')
        for user, extra in [
            (self.planner, ['add_cyclecountabcpolicy', 'view_cyclecountabcpolicy']),
            (
                self.reviewer,
                ['approve_cyclecountapproval', 'approve_cyclecountabcpolicy'],
            ),
            (self.other, ['approve_cyclecountapproval', 'approve_cyclecountabcpolicy']),
        ]:
            user.user_permissions.add(
                *Permission.objects.filter(
                    codename__in=['view_stockitem', 'change_stockitem', *extra]
                )
            )
        self.location = StockLocation.objects.create(name='Native ABC policy location')
        self.part = Part.objects.create(name='Native ABC policy material', units='ea')
        self.item = StockItem.objects.create(
            location=self.location, part=self.part, quantity=10
        )
        self.policy = {
            'version': 'manual-policy-1',
            'currency': 'SAR',
            'period': '2026',
            'aShare': '0.8',
            'bShare': '0.95',
            'intervalDays': {'A': 30, 'B': 90, 'C': 365},
        }
        self.values = {str(self.part.pk): '100.00000001'}
        override = override_settings(
            COUNT_REVIEW_POLICY={
                'tenantId': 'synthetic',
                'environment': 'test',
                'policyVersion': 'abc-native-1',
                'reviewerId': self.reviewer.pk,
                'expirySeconds': 3600,
            }
        )
        override.enable()
        self.addCleanup(override.disable)
        self.deployment = installation_policy()

    def propose(self, command='proposal-1'):
        """Submit one exact manually sourced candidate through native services."""
        return propose_abc_policy(
            self.location.pk,
            self.planner,
            self.policy,
            self.values,
            'manual-reconciled-register-2026',
            command,
            self.deployment,
        )

    def test_independent_review_preserves_stock_and_manual_source_qualification(self):
        """Approval persists evidence without manufacturing native economic authority."""
        before = StockItemTracking.objects.count()
        proposal = self.propose()
        decision = decide_abc_policy(
            proposal['policyId'],
            self.reviewer,
            0,
            'APPROVED',
            'Reviewed manual register in SAR for declared period',
            'approve-1',
            self.deployment,
        )
        self.assertEqual(decision['state'], 'APPROVED')
        self.assertEqual(decision['revision'], 1)
        self.assertEqual(
            decision['sourceQualification'], 'OPERATOR_INPUT_NOT_NATIVE_INGESTION'
        )
        self.assertEqual(decision['stockEffect'], 'NONE')
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.quantity), '10.00000')
        self.assertEqual(StockItemTracking.objects.count(), before)
        self.assertEqual(
            CycleCountABCPolicy.objects.get(pk=proposal['policyId']).decision_history[
                0
            ]['actorId'],
            str(self.reviewer.pk),
        )

    def test_native_identity_replays_only_exact_payload(self):
        """Duplicate identities preserve the record; changed payload conflicts."""
        original = self.propose()
        self.assertEqual(self.propose()['policyId'], original['policyId'])
        self.values[str(self.part.pk)] = '101'
        with self.assertRaises(CountConflictError):
            self.propose()
        self.assertEqual(CycleCountABCPolicy.objects.count(), 1)

    def test_current_permission_and_assigned_reviewer_rechecked(self):
        """Warm native actors cannot keep revoked authority or impersonate review."""
        proposal = self.propose()
        for actor in [self.planner, self.other]:
            with self.assertRaises(PermissionDenied):
                decide_abc_policy(
                    proposal['policyId'],
                    actor,
                    0,
                    'APPROVED',
                    'Reviewed',
                    'approve-other',
                    self.deployment,
                )
        self.reviewer.user_permissions.remove(
            Permission.objects.get(codename='approve_cyclecountabcpolicy')
        )
        with self.assertRaises(PermissionDenied):
            read_abc_policy(proposal['policyId'], self.reviewer, self.deployment)

    def test_policy_revision_and_material_identity_cannot_drift(self):
        """Approval binds current native units and exact independent revision."""
        proposal = self.propose()
        with self.assertRaises(CountConflictError):
            decide_abc_policy(
                proposal['policyId'],
                self.reviewer,
                1,
                'APPROVED',
                'Reviewed',
                'stale-revision',
                self.deployment,
            )
        self.part.units = 'kg'
        self.part.save()
        with self.assertRaises(CountConflictError):
            decide_abc_policy(
                proposal['policyId'],
                self.reviewer,
                0,
                'APPROVED',
                'Reviewed',
                'changed-unit',
                self.deployment,
            )

    def test_api_rejects_authority_injection_and_default_disabled_routes(self):
        """Server-selected deployment and reviewer cannot enter caller payloads."""
        client = APIClient()
        client.force_login(self.planner)
        route = reverse('api-cycle-count-abc-policy-create')
        payload = {
            'location_id': self.location.pk,
            'policy': self.policy,
            'annual_usage_values': self.values,
            'source_reference': 'manual-register',
            'command_id': 'api-proposal',
        }
        self.assertEqual(
            client.post(
                route, {**payload, 'reviewerId': self.planner.pk}, format='json'
            ).status_code,
            400,
        )
        self.assertEqual(client.post(route, payload, format='json').status_code, 200)
        with override_settings(COUNT_REVIEW_POLICY=None):
            self.assertEqual(
                client.post(route, payload, format='json').status_code, 404
            )

    def test_self_review_and_changed_replay_decision_are_denied(self):
        """One user cannot propose and approve; replay identity binds the reason."""
        self.reviewer.user_permissions.add(
            Permission.objects.get(codename='add_cyclecountabcpolicy')
        )
        with self.assertRaises(PermissionDenied):
            propose_abc_policy(
                self.location.pk,
                self.reviewer,
                self.policy,
                self.values,
                'manual-register',
                'self-proposal',
                self.deployment,
            )
        proposal = self.propose()
        first = decide_abc_policy(
            proposal['policyId'],
            self.reviewer,
            0,
            'APPROVED',
            'Reviewed source',
            'approval',
            self.deployment,
        )
        self.assertEqual(
            decide_abc_policy(
                proposal['policyId'],
                self.reviewer,
                0,
                'APPROVED',
                'Reviewed source',
                'approval',
                self.deployment,
            ),
            first,
        )
        with self.assertRaises(CountConflictError):
            decide_abc_policy(
                proposal['policyId'],
                self.reviewer,
                0,
                'APPROVED',
                'Changed reason',
                'approval',
                self.deployment,
            )

    def test_authoritative_lookup_recognizes_retained_operations_without_dispatch(self):
        """Lost acknowledgements resolve by current permissioned native reads."""
        proposal = self.propose()
        before = CycleCountABCPolicy.objects.count()
        resolved = lookup_abc_proposal(self.planner, 'proposal-1', self.deployment)
        self.assertEqual(resolved['receipt'], proposal)
        self.assertEqual(resolved['commandId'], 'proposal-1')
        decide_abc_policy(
            proposal['policyId'],
            self.reviewer,
            0,
            'APPROVED',
            'Source reviewed',
            'decision-1',
            self.deployment,
        )
        decide_abc_policy(
            proposal['policyId'],
            self.reviewer,
            1,
            'REVOKED',
            'Source invalidated',
            'revoke-1',
            self.deployment,
        )
        resolved = lookup_abc_decision(
            proposal['policyId'], self.reviewer, 'decision-1', self.deployment
        )
        self.assertEqual(resolved['recordedDecision']['decision'], 'APPROVED')
        self.assertEqual(resolved['receipt']['state'], 'REVOKED')
        self.assertEqual(CycleCountABCPolicy.objects.count(), before)
        with self.assertRaises(PermissionDenied):
            lookup_abc_decision(
                proposal['policyId'], self.other, 'decision-1', self.deployment
            )

    def test_context_and_governed_schedule_keep_approval_and_count_effects_separate(
        self,
    ):
        """Context exposes permitted material IDs; only approved policies schedule."""
        proposal = self.propose()
        context = abc_policy_context(self.location.pk, self.planner, self.deployment)
        self.assertEqual(context['actor']['role'], 'PLANNER')
        self.assertEqual(context['parts'][0]['id'], self.part.pk)
        self.assertEqual(context['policies'][0]['id'], proposal['policyId'])
        with self.assertRaises(CountConflictError):
            approved_abc_schedule(proposal['policyId'], self.reviewer, self.deployment)
        decide_abc_policy(
            proposal['policyId'],
            self.reviewer,
            0,
            'APPROVED',
            'Reviewed manual source',
            'schedule-approval',
            self.deployment,
        )
        schedule = approved_abc_schedule(
            proposal['policyId'], self.reviewer, self.deployment
        )
        self.assertEqual(schedule['schedule']['materials'][0]['due'], 'YES')
        self.assertEqual(
            schedule['sourceQualification'], 'OPERATOR_INPUT_NOT_NATIVE_INGESTION'
        )
        self.item.refresh_from_db()
        self.assertEqual(str(self.item.quantity), '10.00000')

    def test_api_lookup_and_decision_require_native_csrf_and_exact_revision(self):
        """Reads recognize exact commands; submissions retain native CSRF checks."""
        proposal = self.propose()
        client = APIClient()
        client.force_login(self.planner)
        lookup = reverse('api-cycle-count-abc-policy-create')
        response = client.get(lookup, {'command_id': 'proposal-1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['receipt']['policyId'], proposal['policyId'])
        self.assertEqual(client.get(lookup, {'command_id': 'missing'}).status_code, 404)
        self.assertEqual(
            client.get(
                lookup, {'command_id': 'proposal-1', 'reviewerId': self.reviewer.pk}
            ).status_code,
            400,
        )
        route = reverse(
            'api-cycle-count-abc-policy-record', kwargs={'pk': proposal['policyId']}
        )
        client.force_login(self.reviewer)
        payload = {
            'expected_revision': 0,
            'command_id': 'api-decision',
            'decision': 'APPROVED',
            'reason': 'Manual source checked',
        }
        self.assertEqual(
            client.post(
                route, {**payload, 'expected_revision': True}, format='json'
            ).status_code,
            400,
        )
        csrf = APIClient(enforce_csrf_checks=True)
        csrf.force_login(self.reviewer)
        self.assertEqual(csrf.post(route, payload, format='json').status_code, 403)
        self.assertEqual(client.post(route, payload, format='json').status_code, 200)
        result = client.get(route, {'command_id': 'api-decision'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['recordedDecision']['decision'], 'APPROVED')
        self.assertEqual(CycleCountABCPolicy.objects.count(), 1)

    def test_source_qualification_cannot_be_promoted_by_record_drift(self):
        """Manual source remains manual even if an external writer alters metadata."""
        proposal = self.propose()
        CycleCountABCPolicy.objects.filter(pk=proposal['policyId']).update(
            source_qualification='NATIVE_INGESTED'
        )
        with self.assertRaises(CountConflictError):
            read_abc_policy(proposal['policyId'], self.reviewer, self.deployment)
