"""Opt-in standalone C03 authority; no endpoint or deployment activates it."""

from datetime import datetime
from datetime import timezone as datetime_timezone

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from stock.counting import CountConflictError, evidence_hash, variance
from stock.counting_service import (
    _authorize,
    _binding,
    _ownership,
    _snapshot,
    commit_count,
    request_count_review,
)
from stock.models import CycleCountApproval, CycleCountSession, StockItem


def _decision_actor(user, permission='approve'):
    current = _authorize(user)
    if not current.has_perm(f'stock.{permission}_cyclecountapproval'):
        raise PermissionDenied
    return current


def _scope(session, user, check_material=True):
    if len(session.observations) != len(session.scope):
        raise CountConflictError('Every frozen item requires an observation')
    items = list(
        StockItem.objects.select_for_update()
        .filter(pk__in=[row['item'] for row in session.scope])
        .order_by('pk')
    )
    if len(items) != len(session.scope):
        raise CountConflictError('Frozen stock no longer exists; recount required')
    _ownership(user, session.location, items)
    by_id = {str(item.pk): item for item in items}
    for row in session.scope:
        item = by_id[row['item']]
        if not check_material:
            continue
        if item.serialized or not item.in_stock:
            raise CountConflictError('Stock eligibility changed; recount required')
        variance(row, session.observations[row['item']], _snapshot(item))
    return items


def _exact(
    approval, session, tenant_id, environment, policy_version, check_expiry=True
):
    if approval.binding != _binding(session, tenant_id, environment, policy_version):
        raise CountConflictError('Approval material binding changed')
    if check_expiry and approval.expires_at <= timezone.now():
        raise CountConflictError('Approval expired')
    if not approval.decision_history:
        raise CountConflictError('Approval request audit missing')
    event = approval.decision_history[0]
    payload = approval.commands.get(event.get('commandId'))
    if (
        event.get('decision') != 'REQUESTED'
        or event.get('actor') != str(session.requester_id)
        or not isinstance(payload, dict)
        or payload.get('binding') != approval.binding
        or payload.get('reviewer') != str(approval.reviewer_id)
        or payload.get('expiresAt') != approval.expires_at.isoformat()
        or event.get('payloadHash') != evidence_hash(payload)
    ):
        raise CountConflictError('Approval request audit conflict')


@transaction.atomic
def request_native_review(
    session_id,
    user,
    expected_revision,
    tenant_id,
    environment,
    policy_version,
    reviewer,
    expires_at,
    command_id,
):
    """Persist one explicitly assigned reviewer and server-selected expiry/policy."""
    user = _authorize(user, 'view', 'change')
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    if session.requester_id != user.pk:
        raise PermissionDenied
    reviewer = _decision_actor(reviewer)
    if reviewer.pk == user.pk:
        raise PermissionDenied
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        raise CountConflictError('Invalid command identity')
    if not isinstance(expires_at, datetime) or not timezone.is_aware(expires_at):
        raise CountConflictError('Explicit future policy expiry required')
    expires_at = expires_at.astimezone(datetime_timezone.utc)
    approval = (
        CycleCountApproval.objects.select_for_update().filter(session=session).first()
    )
    _scope(session, user, check_material=approval is None)
    _scope(session, reviewer, check_material=approval is None)
    binding = _binding(session, tenant_id, environment, policy_version)
    payload = {
        'kind': 'request',
        'binding': binding,
        'reviewer': str(reviewer.pk),
        'expiresAt': expires_at.isoformat(),
        'expectedRevision': str(expected_revision),
    }
    if approval:
        _exact(
            approval,
            session,
            tenant_id,
            environment,
            policy_version,
            check_expiry=False,
        )
        if approval.commands.get(command_id) != payload:
            raise CountConflictError('Approval request command conflict')
        return {
            'requestId': approval.pk,
            'state': approval.state,
            'revision': approval.revision,
        }
    if expires_at <= timezone.now():
        raise CountConflictError('Explicit future policy expiry required')

    def create(binding, actor):
        approval = CycleCountApproval.objects.create(
            session=session,
            binding=binding,
            reviewer=reviewer,
            expires_at=expires_at,
            commands={command_id: payload},
            decision_history=[
                {
                    'actor': str(actor.pk),
                    'decision': 'REQUESTED',
                    'reason': '',
                    'at': timezone.now().isoformat(),
                    'commandId': command_id,
                    'payloadHash': evidence_hash(payload),
                }
            ],
        )
        return str(approval.pk)

    request_count_review(
        session_id,
        user,
        expected_revision,
        tenant_id,
        environment,
        policy_version,
        create,
    )
    approval = CycleCountApproval.objects.get(session=session)
    return {
        'requestId': approval.pk,
        'state': approval.state,
        'revision': approval.revision,
    }


@transaction.atomic
def decide_native_review(
    session_id,
    user,
    expected_revision,
    command_id,
    decision,
    tenant_id,
    environment,
    policy_version,
    reason='',
):
    """Compare-and-swap an independent decision; approval never moves stock."""
    if decision not in ('APPROVED', 'REJECTED', 'REVOKED'):
        raise CountConflictError('Invalid approval decision')
    if (
        not isinstance(reason, str)
        or len(reason) > 255
        or any(ord(char) < 32 for char in reason)
    ):
        raise CountConflictError('Decision reason must be bounded plain text')
    if decision in ('REJECTED', 'REVOKED') and not reason.strip():
        raise CountConflictError('Rejected or revoked decisions require a reason')
    user = _decision_actor(user, 'revoke' if decision == 'REVOKED' else 'approve')
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    approval = CycleCountApproval.objects.select_for_update().get(session=session)
    if session.requester_id == user.pk or (
        decision != 'REVOKED' and approval.reviewer_id != user.pk
    ):
        raise PermissionDenied
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        raise CountConflictError('Invalid command identity')
    _exact(
        approval, session, tenant_id, environment, policy_version, check_expiry=False
    )
    _scope(session, user, check_material=False)
    payload = {
        'kind': 'decision',
        'decision': decision,
        'actor': str(user.pk),
        'reason': reason,
        'expectedRevision': str(expected_revision),
        'binding': approval.binding,
        'requestId': str(approval.pk),
    }
    if command_id in approval.commands:
        if approval.commands[command_id] != payload:
            raise CountConflictError('Approval decision command conflict')
        return {
            'requestId': approval.pk,
            'state': approval.state,
            'revision': approval.revision,
        }
    if approval.revision != expected_revision or session.state != 'REVIEW':
        raise CountConflictError('Approval revision conflict')
    _exact(approval, session, tenant_id, environment, policy_version)
    _scope(session, user)
    allowed = ('PENDING', 'APPROVED') if decision == 'REVOKED' else ('PENDING',)
    if approval.state not in allowed:
        raise CountConflictError('Approval decision conflict')
    approval.state = decision
    approval.revision += 1
    approval.commands[command_id] = payload
    approval.decision_history.append(
        {
            'actor': str(user.pk),
            'decision': decision,
            'reason': reason,
            'at': timezone.now().isoformat(),
            'commandId': command_id,
            'payloadHash': evidence_hash(payload),
            'revision': str(approval.revision),
        }
    )
    approval.save()
    return {
        'requestId': approval.pk,
        'state': approval.state,
        'revision': approval.revision,
    }


@transaction.atomic
def commit_native_count(
    session_id, user, command_id, tenant_id, environment, policy_version
):
    """Consume persisted native approval and stocktake in the same transaction."""
    # Lock order is session -> approval -> sorted stock items for all authority commands.
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    approval = CycleCountApproval.objects.select_for_update().get(session=session)

    def validate(request_id, binding, actor, action):
        _exact(approval, session, tenant_id, environment, policy_version)
        if (
            str(approval.pk) != request_id
            or action != 'stocktake'
            or approval.binding != binding
        ):
            raise CountConflictError('Approval request binding conflict')
        if approval.state != 'APPROVED' or approval.reviewer_id is None:
            raise CountConflictError('Count is not independently approved')
        event = approval.decision_history[-1]
        payload = approval.commands.get(event.get('commandId'))
        if (
            event.get('decision') != 'APPROVED'
            or event.get('actor') != str(approval.reviewer_id)
            or event.get('revision') != str(approval.revision)
            or not isinstance(payload, dict)
            or payload.get('decision') != 'APPROVED'
            or payload.get('actor') != str(approval.reviewer_id)
            or payload.get('binding') != binding
            or payload.get('requestId') != request_id
            or event.get('payloadHash') != evidence_hash(payload)
        ):
            raise CountConflictError('Approval decision audit conflict')
        reviewer = _decision_actor(approval.reviewer)
        if reviewer.pk == session.requester_id:
            raise PermissionDenied
        _scope(session, reviewer)
        approval.state = 'CONSUMED'
        approval.revision += 1
        approval.decision_history.append(
            {
                'actor': str(actor.pk),
                'decision': 'CONSUMED',
                'reason': '',
                'at': timezone.now().isoformat(),
                'commandId': command_id,
                'payloadHash': evidence_hash(
                    {'binding': binding, 'commandId': command_id}
                ),
            }
        )
        approval.save()
        return True

    return commit_count(
        session_id, user, command_id, tenant_id, environment, policy_version, validate
    )
