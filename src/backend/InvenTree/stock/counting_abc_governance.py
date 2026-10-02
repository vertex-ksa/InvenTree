"""Persisted ABC proposals; manual review never becomes stock authority."""

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from InvenTree.helpers import current_date
from InvenTree.status_codes import StockHistoryCode
from stock.counting import CountConflictError, evidence_hash
from stock.counting_abc import abc_count_schedule
from stock.counting_approval import _decision_actor
from stock.counting_service import _authorize, _lock_parts, _ownership
from stock.models import (
    CycleCountABCPolicy,
    StockItem,
    StockItemTracking,
    StockLocation,
)


def _text(value, limit=128):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(char) < 32 for char in value)
    ):
        raise CountConflictError('A bounded source or command reference is required')
    return value


def _scope(location_id, user):
    current = _authorize(user)
    current = _authorize(current, 'view')
    location = StockLocation.objects.get(pk=location_id)
    items = list(
        StockItem.objects.select_for_update().filter(location=location).order_by('pk')
    )
    _ownership(current, location, items)
    _lock_parts(items)
    if not items or any(not item.in_stock for item in items):
        raise CountConflictError('ABC native material scope is unavailable')
    return (
        current,
        location,
        [
            {
                'item': str(item.pk),
                'part': str(item.part_id),
                'location': str(item.location_id),
                'partRevision': item.part.revision or '',
                'unit': item.part.units or '',
            }
            for item in items
        ],
    )


def _binding(deployment, scope):
    return {
        'domain': 'abc-policy-authority/1',
        'tenantId': deployment['tenantId'],
        'environment': deployment['environment'],
        'installationPolicy': deployment['effectiveVersion'],
        'reviewerId': str(deployment['reviewerId']),
        'materials': scope,
    }


def _evidence(policy, values, reference, binding):
    return {
        'policy': {
            **policy,
            'intervalDays': {
                key: str(value) for key, value in policy['intervalDays'].items()
            },
        },
        'annualUsageValues': values,
        'sourceReference': reference,
        'sourceQualification': 'OPERATOR_INPUT_NOT_NATIVE_INGESTION',
        'binding': binding,
    }


def policy_record(record):
    """Serialize a narrow policy record without native stock balances."""
    return {
        'policyId': record.pk,
        'locationId': record.location_id,
        'requesterId': record.requester_id,
        'reviewerId': record.reviewer_id,
        'state': record.state,
        'revision': record.revision,
        'policy': record.policy,
        'annualUsageValues': record.annual_usage_values,
        'sourceReference': record.source_reference,
        'sourceQualification': record.source_qualification,
        'proposalHash': record.proposal_hash,
        'stockEffect': 'NONE',
    }


def _request_key(actor_id, command_id, deployment):
    return evidence_hash(
        {
            'domain': 'abc-policy-request/1',
            'tenantId': deployment['tenantId'],
            'environment': deployment['environment'],
            'actorId': str(actor_id),
            'commandId': _text(command_id),
        }
    )


@transaction.atomic
def abc_policy_context(location_id, user, deployment):
    """Return current economically permissioned native material selections."""
    actor, location, scope = _scope(location_id, user)
    if actor.pk == deployment['reviewerId']:
        actor = _decision_actor(actor)
        if not actor.has_perm('stock.approve_cyclecountabcpolicy'):
            raise PermissionDenied
        role = 'REVIEWER'
    else:
        if not actor.has_perm('stock.add_cyclecountabcpolicy') or not actor.has_perm(
            'stock.view_cyclecountabcpolicy'
        ):
            raise PermissionDenied
        role = 'PLANNER'
    records = (
        CycleCountABCPolicy.objects.filter(location=location)
        .filter(Q(requester=actor) | Q(reviewer=actor))
        .order_by('pk')
    )
    if records.count() > 1000:
        raise CountConflictError('Policy selection capacity exceeded')
    from part.models import Part

    parts = Part.objects.filter(pk__in={row['part'] for row in scope}).order_by('pk')
    return {
        'actor': {'id': actor.pk, 'username': actor.username, 'role': role},
        'location': {'id': location.pk, 'name': location.name},
        'itemIds': [int(row['item']) for row in scope],
        'parts': [
            {'id': part.pk, 'name': part.name, 'unit': part.units or ''}
            for part in parts
        ],
        'policies': [
            {
                'id': record.pk,
                'state': record.state,
                'revision': record.revision,
                'requesterId': record.requester_id,
                'reviewerId': record.reviewer_id,
                'materialCurrent': record.binding == _binding(deployment, scope),
            }
            for record in records
        ],
        'sourceQualification': 'OPERATOR_INPUT_NOT_NATIVE_INGESTION',
        'stockEffect': 'NONE',
    }


@transaction.atomic
def approved_abc_schedule(policy_id, user, deployment):
    """Use a reviewed manual policy; native count history supplies freshness."""
    receipt = read_abc_policy(policy_id, user, deployment)
    if receipt['state'] != 'APPROVED':
        raise CountConflictError(
            'Only an approved policy can produce a governed schedule'
        )
    record = CycleCountABCPolicy.objects.get(pk=policy_id)
    _, _, scope = _scope(record.location_id, user)
    latest = dict(
        StockItemTracking.objects.filter(
            item_id__in=[row['item'] for row in scope],
            tracking_type=StockHistoryCode.STOCK_COUNT,
        )
        .values('item_id')
        .annotate(last=Max('date'))
        .values_list('item_id', 'last')
    )
    rows = []
    for part in sorted(record.annual_usage_values, key=int):
        dates = [latest.get(int(row['item'])) for row in scope if row['part'] == part]
        oldest = min(dates) if dates and all(dates) else None
        last_date = (
            (timezone.localtime(oldest) if timezone.is_aware(oldest) else oldest)
            .date()
            .isoformat()
            if oldest
            else None
        )
        rows.append(
            {
                'partId': part,
                'annualUsageValue': record.annual_usage_values[part],
                'lastCountDate': last_date,
            }
        )
    return {
        'policyId': policy_id,
        'sourceQualification': receipt['sourceQualification'],
        'proposalHash': record.proposal_hash,
        'schedule': abc_count_schedule(rows, record.policy, current_date()),
        'stockEffect': 'NONE',
    }


@transaction.atomic
def lookup_abc_proposal(user, command_id, deployment):
    """Authoritative read of one retained planner identity without resubmission."""
    actor = _authorize(user)
    actor = _authorize(actor, 'view')
    if not actor.has_perm('stock.view_cyclecountabcpolicy'):
        raise PermissionDenied
    record = CycleCountABCPolicy.objects.get(
        request_key=_request_key(actor.pk, command_id, deployment)
    )
    return {
        'commandId': command_id,
        'receipt': read_abc_policy(record.pk, actor, deployment),
    }


@transaction.atomic
def lookup_abc_decision(policy_id, user, command_id, deployment):
    """Recognize a recorded decision while separately returning current state."""
    receipt = read_abc_policy(policy_id, user, deployment)
    actor = _decision_actor(user)
    record = CycleCountABCPolicy.objects.get(pk=policy_id)
    if actor.pk != record.reviewer_id or actor.pk == record.requester_id:
        raise PermissionDenied
    command_id = _text(command_id)
    if command_id not in record.commands:
        raise ObjectDoesNotExist
    payload = record.commands[command_id]
    if not any(
        event.get('actorId') == str(actor.pk)
        and event.get('commandId') == command_id
        and event.get('payloadHash') == evidence_hash(payload)
        for event in record.decision_history
    ):
        raise CountConflictError('ABC decision audit evidence changed')
    return {'commandId': command_id, 'recordedDecision': payload, 'receipt': receipt}


@transaction.atomic
def propose_abc_policy(
    location_id, user, policy, values, reference, command_id, deployment
):
    """Freeze an explicitly permissioned planner's exact economic proposal."""
    actor, location, scope = _scope(location_id, user)
    if not actor.has_perm('stock.add_cyclecountabcpolicy'):
        raise PermissionDenied
    reviewer = _decision_actor(User.objects.get(pk=deployment['reviewerId']))
    if reviewer.pk == actor.pk or not reviewer.has_perm(
        'stock.approve_cyclecountabcpolicy'
    ):
        raise PermissionDenied
    reference = _text(reference, 255)
    command_id = _text(command_id)
    parts = {item['part'] for item in scope}
    if not isinstance(values, dict) or set(values) != parts:
        raise CountConflictError('Proposal must cover every current native material')
    abc_count_schedule(
        [
            {'partId': part, 'annualUsageValue': values[part], 'lastCountDate': None}
            for part in sorted(parts, key=int)
        ],
        policy,
        current_date(),
    )
    binding = _binding(deployment, scope)
    proposal_hash = evidence_hash(_evidence(policy, values, reference, binding))
    request_key = _request_key(actor.pk, command_id, deployment)
    record, created = CycleCountABCPolicy.objects.get_or_create(
        request_key=request_key,
        defaults={
            'location': location,
            'requester': actor,
            'reviewer': reviewer,
            'policy': policy,
            'annual_usage_values': values,
            'source_reference': reference,
            'binding': binding,
            'proposal_hash': proposal_hash,
        },
    )
    if not created and record.proposal_hash != proposal_hash:
        raise CountConflictError('Retained proposal identity has a different payload')
    return policy_record(record)


@transaction.atomic
def read_abc_policy(policy_id, user, deployment):
    """Authorize the current planner or assigned independent reviewer."""
    record = CycleCountABCPolicy.objects.select_for_update().get(pk=policy_id)
    actor, _, scope = _scope(record.location_id, user)
    if actor.pk == record.requester_id:
        if not actor.has_perm('stock.view_cyclecountabcpolicy'):
            raise PermissionDenied
    elif actor.pk == record.reviewer_id:
        actor = _decision_actor(actor)
        if not actor.has_perm('stock.approve_cyclecountabcpolicy'):
            raise PermissionDenied
    else:
        raise PermissionDenied
    if record.source_qualification != 'OPERATOR_INPUT_NOT_NATIVE_INGESTION':
        raise CountConflictError('ABC economic source qualification changed')
    if record.binding != _binding(
        deployment, scope
    ) or record.proposal_hash != evidence_hash(
        _evidence(
            record.policy,
            record.annual_usage_values,
            record.source_reference,
            record.binding,
        )
    ):
        raise CountConflictError('Native ABC authority or material evidence changed')
    return policy_record(record)


@transaction.atomic
def decide_abc_policy(
    policy_id, user, expected_revision, decision, reason, command_id, deployment
):
    """Persist independent CAS decisions; do not open or commit a count."""
    record = CycleCountABCPolicy.objects.select_for_update().get(pk=policy_id)
    current = read_abc_policy(policy_id, user, deployment)
    actor = _decision_actor(user)
    if (
        actor.pk != record.reviewer_id
        or actor.pk == record.requester_id
        or not actor.has_perm('stock.approve_cyclecountabcpolicy')
    ):
        raise PermissionDenied
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or decision not in ('APPROVED', 'REJECTED', 'REVOKED')
    ):
        raise CountConflictError('Invalid policy decision')
    reason, command_id = _text(reason, 255), _text(command_id)
    payload = {
        'revision': str(expected_revision),
        'decision': decision,
        'reason': reason,
        'proposalHash': record.proposal_hash,
    }
    if command_id in record.commands:
        if record.commands[command_id] != payload:
            raise CountConflictError('Retained policy decision identity changed')
        return current
    if (
        record.revision != expected_revision
        or (decision == 'REVOKED' and record.state not in ('PENDING', 'APPROVED'))
        or (decision != 'REVOKED' and record.state != 'PENDING')
    ):
        raise CountConflictError('Native policy state or revision changed')
    record.commands[command_id] = payload
    record.decision_history.append(
        {
            'actorId': str(actor.pk),
            'commandId': command_id,
            'payloadHash': evidence_hash(payload),
            'at': timezone.now().isoformat(),
        }
    )
    record.state = decision
    record.revision += 1
    record.save(update_fields=['commands', 'decision_history', 'state', 'revision'])
    return policy_record(record)
