"""Default-disabled, authenticated standalone count commands and masked reads."""

from collections.abc import Mapping
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotFound
from django.urls import path
from django.utils import timezone

from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from stock.counting import CountConflictError, evidence_hash, variance
from stock.counting_approval import (
    _decision_actor,
    _exact,
    _scope,
    commit_native_count,
    decide_native_review,
    request_native_review,
)
from stock.counting_records import observer_record
from stock.counting_service import (
    _authorize_observer,
    _require_material_scope,
    _snapshot,
    observe_count,
    open_count,
)
from stock.models import CycleCountApproval, CycleCountSession, StockLocation


class CountConflict(APIException):
    """Bounded errors never serialize hidden count facts."""

    status_code = 409
    default_detail = 'Count state changed; refresh or start a new count.'


class CountPolicyUnavailable(APIException):
    """Fail closed for malformed installation policy."""

    status_code = 503
    default_detail = 'Count policy is unavailable.'


def installation_policy():
    """Only server settings can select authority, deployment identity and expiry."""
    config = getattr(settings, 'COUNT_REVIEW_POLICY', None)
    keys = {'tenantId', 'environment', 'policyVersion', 'reviewerId', 'expirySeconds'}
    if not isinstance(config, dict) or set(config) != keys:
        raise CountPolicyUnavailable
    for key in ('tenantId', 'environment', 'policyVersion'):
        if (
            not isinstance(config[key], str)
            or not config[key].strip()
            or len(config[key]) > 128
            or any(ord(char) < 32 for char in config[key])
        ):
            raise CountPolicyUnavailable
    for key in ('reviewerId', 'expirySeconds'):
        if type(config[key]) is not int or config[key] <= 0:
            raise CountPolicyUnavailable
    try:
        timezone.now() + timedelta(seconds=config['expirySeconds'])
    except OverflowError as error:
        raise CountPolicyUnavailable from error
    fingerprint = evidence_hash(
        {'domain': 'count-policy-config/1', **{k: str(v) for k, v in config.items()}}
    )
    return {**config, 'effectiveVersion': f'{config["policyVersion"]}/{fingerprint}'}


class StrictInput(serializers.Serializer):
    """DRF ordinarily ignores unknown fields; authority fields must be rejected."""

    def to_internal_value(self, data):
        """Validate the exact permitted command boundary."""
        if not isinstance(data, Mapping) or set(data) - set(self.fields):
            raise serializers.ValidationError(
                {'non_field_errors': ['Unexpected command fields.']}
            )
        for key, field in self.fields.items():
            if isinstance(field, serializers.CharField) and key in data:
                if not isinstance(data[key], str):
                    raise serializers.ValidationError(
                        {'non_field_errors': ['Command text must be strings.']}
                    )
            if isinstance(field, serializers.IntegerField) and key in data:
                if type(data[key]) is not int:
                    raise serializers.ValidationError(
                        {
                            'non_field_errors': [
                                'Revision and identifiers must be integers.'
                            ]
                        }
                    )
        return super().to_internal_value(data)


class OpenInput(StrictInput):
    """An authorized native location is the complete first-slice scope."""

    location_id = serializers.IntegerField(min_value=1)


class CommandInput(StrictInput):
    """Exact command identity enables retained acknowledgements."""

    command_id = serializers.CharField(max_length=128, trim_whitespace=False)


class RevisionInput(CommandInput):
    """Count and approval revisions remain independent CAS values."""

    expected_revision = serializers.IntegerField(min_value=0)


class ObserveInput(RevisionInput):
    """Quantities are strings so native decimal precision is never guessed."""

    item_id = serializers.IntegerField(min_value=1)
    quantity = serializers.CharField(max_length=32, trim_whitespace=False)

    def validate_quantity(self, value):
        """Validate the exact permitted command boundary."""
        # CharField otherwise accepts numeric JSON by coercion.
        if not isinstance(self.initial_data.get('quantity'), str):
            raise serializers.ValidationError('Enter an exact decimal string.')
        return value


class DecisionInput(RevisionInput):
    """Approval never commits stock; revocation/rejection preserve evidence."""

    decision = serializers.ChoiceField(choices=['APPROVED', 'REJECTED', 'REVOKED'])
    reason = serializers.CharField(max_length=255, allow_blank=True, default='')


class CountView(APIView):
    """Disabled404 precedes authentication and OPTIONS for every route/verb."""

    permission_classes = [IsAuthenticated]
    # This local first slice requires native browser sessions with native CSRF.
    # OAuth/token delegation is outside its authority policy.
    authentication_classes = [SessionAuthentication]

    def dispatch(self, request, *args, **kwargs):
        """Validate the exact permitted command boundary."""
        if getattr(settings, 'COUNT_REVIEW_POLICY', None) is None:
            return HttpResponseNotFound()
        return super().dispatch(request, *args, **kwargs)

    def handle_exception(self, exc):
        """Validate the exact permitted command boundary."""
        if isinstance(exc, CountConflictError):
            exc = CountConflict()
        elif isinstance(exc, ObjectDoesNotExist):
            exc = NotFound('Count scope is unavailable.')
        return super().handle_exception(exc)

    def command(self, serializer_type):
        """Validate the exact permitted command boundary."""
        serializer = serializer_type(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class CountOpen(CountView):
    """Start a masked count without stock read or stock mutation grants."""

    def get(self, request):
        """List ownership-scoped location labels without inventory balances."""
        installation_policy()
        actor = _authorize_observer(request.user, 'add')
        locations = list(StockLocation.objects.order_by('name', 'pk')[:1001])
        if len(locations) > 1000:
            raise CountConflictError('Count location selection capacity exceeded')
        return Response(
            {
                'locations': [
                    {'id': location.pk, 'name': location.name}
                    for location in locations
                    if location.check_ownership(actor)
                ]
            }
        )

    def post(self, request):
        """Apply current native permissions and trusted server policy."""
        installation_policy()
        data = self.command(OpenInput)
        session = open_count(
            StockLocation.objects.get(pk=data['location_id']), request.user
        )
        return Response(observer_record(session.pk, request.user), status=201)


class CountObserve(CountView):
    """Read the blind task or retain one immutable observation."""

    def get(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        installation_policy()
        return Response(observer_record(pk, request.user))

    def post(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        installation_policy()
        data = self.command(ObserveInput)
        observe_count(
            pk,
            request.user,
            data['item_id'],
            data['quantity'],
            data['command_id'],
            data['expected_revision'],
        )
        return Response(observer_record(pk, request.user))


class CountRequest(CountView):
    """Choose native reviewer and expiry only from trusted installation policy."""

    @transaction.atomic
    def post(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        policy = installation_policy()
        data = self.command(RevisionInput)
        # Stable session -> approval locks retain the original server expiry on ACK.
        session = CycleCountSession.objects.select_for_update().get(pk=pk)
        approval = (
            CycleCountApproval.objects.select_for_update()
            .filter(session=session)
            .first()
        )
        expiry = (
            approval.expires_at
            if approval
            else timezone.now() + timedelta(seconds=policy['expirySeconds'])
        )
        receipt = request_native_review(
            pk,
            request.user,
            data['expected_revision'],
            policy['tenantId'],
            policy['environment'],
            policy['effectiveVersion'],
            User.objects.get(pk=policy['reviewerId']),
            expiry,
            data['command_id'],
        )
        return Response(receipt)


class CountReview(CountView):
    """Only the assigned current native reviewer can inspect bound evidence."""

    @transaction.atomic
    def get(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        policy = installation_policy()
        actor = _decision_actor(request.user)
        session = CycleCountSession.objects.select_for_update().get(pk=pk)
        approval = CycleCountApproval.objects.select_for_update().get(session=session)
        if actor.pk != approval.reviewer_id or actor.pk == session.requester_id:
            raise PermissionDenied
        _exact(
            approval,
            session,
            policy['tenantId'],
            policy['environment'],
            policy['effectiveVersion'],
            check_expiry=False,
        )
        _require_material_scope(session)
        items = _scope(session, actor, check_material=False)
        by_id = {str(item.pk): item for item in items}
        stale = False
        rows = []
        for row in session.scope:
            derived = variance(row, session.observations[row['item']], row)
            rows.append(
                {
                    **{
                        key: row[key]
                        for key in (
                            'item',
                            'part',
                            'partName',
                            'partIPN',
                            'partRevision',
                            'batch',
                            'unit',
                        )
                    },
                    **derived,
                }
            )
            try:
                variance(
                    row,
                    session.observations[row['item']],
                    _snapshot(by_id[row['item']]),
                )
            except CountConflictError:
                stale = session.state != 'COMMITTED'
        return Response(
            {
                'sessionId': pk,
                'location': {'id': session.location_id, 'name': session.location.name},
                'state': session.state,
                'countRevision': session.revision,
                'requestId': approval.pk,
                'approvalRevision': approval.revision,
                'approvalState': 'EXPIRED'
                if approval.state in ('PENDING', 'APPROVED')
                and approval.expires_at <= timezone.now()
                else approval.state,
                'materialStale': stale,
                'policyVersion': policy['policyVersion'],
                'expiresAt': approval.expires_at,
                'binding': approval.binding,
                'items': rows,
            }
        )

    def post(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        policy = installation_policy()
        data = self.command(DecisionInput)
        return Response(
            decide_native_review(
                pk,
                request.user,
                data['expected_revision'],
                data['command_id'],
                data['decision'],
                policy['tenantId'],
                policy['environment'],
                policy['effectiveVersion'],
                reason=data['reason'],
            )
        )


class CountCommit(CountView):
    """A separate explicit command consumes native approval and adjusts stock."""

    def post(self, request, pk):
        """Apply current native permissions and trusted server policy."""
        policy = installation_policy()
        data = self.command(CommandInput)
        return Response(
            commit_native_count(
                pk,
                request.user,
                data['command_id'],
                policy['tenantId'],
                policy['environment'],
                policy['effectiveVersion'],
            )
        )


urlpatterns = [
    path('', CountOpen.as_view(), name='api-cycle-count-open'),
    path('<int:pk>/', CountObserve.as_view(), name='api-cycle-count-observe'),
    path('<int:pk>/request/', CountRequest.as_view(), name='api-cycle-count-request'),
    path('<int:pk>/review/', CountReview.as_view(), name='api-cycle-count-review'),
    path('<int:pk>/commit/', CountCommit.as_view(), name='api-cycle-count-commit'),
]
