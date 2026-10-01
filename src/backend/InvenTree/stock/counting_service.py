"""Opt-in native cycle-count commands, pending an authoritative C03 adapter/UI.

No public endpoint registers this module. Integration must supply a server-owned
approval validator and deployment identity before exposing the commands.
"""

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max

from stock.counting import CountConflictError, evidence_hash, quantity_text, variance
from stock.models import CycleCountSession, StockItem


def _authorize(user):
    current = User.objects.filter(pk=user.pk).first()
    if (
        current is None
        or not current.is_active
        or not current.has_perm('stock.change_stockitem')
    ):
        raise PermissionDenied


def _snapshot(item):
    return {
        'item': str(item.pk),
        'location': str(item.location_id),
        'quantity': quantity_text(str(item.quantity)),
        'tracking': str(item.tracking_info.aggregate(last=Max('pk'))['last'] or 0),
    }


@transaction.atomic
def open_count(location, user):
    """Freeze one location; serialized units require their native separate flow."""
    _authorize(user)
    items = list(
        StockItem.objects.select_for_update().filter(location=location).order_by('pk')
    )
    if not items or any(item.serialized or not item.in_stock for item in items):
        raise CountConflictError(
            'First slice requires nonserialized stock in one location'
        )
    return CycleCountSession.objects.create(
        location=location, requester=user, scope=[_snapshot(item) for item in items]
    )


@transaction.atomic
def observe_count(session_id, user, item_id, quantity, command_id, expected_revision):
    """Capture a blind observation; same command replays, changed payload conflicts."""
    _authorize(user)
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    if session.requester_id != user.pk:
        raise PermissionDenied
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        raise CountConflictError('Invalid command identity')
    item_id = str(item_id)
    payload = {'item': item_id, 'quantity': quantity_text(quantity)}
    if command_id in session.commands:
        if session.commands[command_id] != payload:
            raise CountConflictError('Command payload conflict')
        return session
    if session.state != 'OPEN' or session.revision != expected_revision:
        raise CountConflictError('Count revision conflict')
    if item_id not in {row['item'] for row in session.scope}:
        raise CountConflictError('Item is outside frozen scope')
    if item_id in session.observations:
        raise CountConflictError(
            'Recount requires a new session preserving original evidence'
        )
    session.observations[item_id] = payload['quantity']
    session.commands[command_id] = payload
    session.revision += 1
    session.save()
    return session


def _binding(session, tenant_id, environment, policy_version):
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(environment, str)
        or not environment
        or not isinstance(policy_version, str)
        or not policy_version
    ):
        raise CountConflictError('Trusted deployment identity and policy are required')
    return {
        'tenantId': tenant_id,
        'environment': environment,
        'application': 'inventree',
        'objectType': 'cycle-count',
        'objectId': str(session.pk),
        'objectVersion': str(session.revision),
        'evidenceHash': evidence_hash(
            {'scope': session.scope, 'observations': session.observations}
        ),
        'policyVersion': policy_version,
    }


@transaction.atomic
def request_count_review(
    session_id,
    user,
    expected_revision,
    tenant_id,
    environment,
    policy_version,
    approval_creator=None,
):
    """Freeze completed evidence through a trusted server C03 producer adapter."""
    _authorize(user)
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    if session.requester_id != user.pk:
        raise PermissionDenied
    if session.state != 'OPEN' or session.revision != expected_revision:
        raise CountConflictError('Count revision conflict')
    if len(session.observations) != len(session.scope):
        raise CountConflictError('Every frozen item requires an observation')
    if approval_creator is None:
        raise CountConflictError('Authoritative approval dependency unavailable')
    request_id = approval_creator(
        _binding(session, tenant_id, environment, policy_version), user
    )
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        raise CountConflictError('Invalid authoritative approval request')
    session.approval_request = request_id
    session.state = 'REVIEW'
    session.save()
    return session


@transaction.atomic
def commit_count(
    session_id,
    user,
    command_id,
    tenant_id,
    environment,
    policy_version,
    approval_validator=None,
):
    """Revalidate evidence and approval, then atomically use native stocktake.

    approval_validator is a trusted server adapter, never an HTTP receipt. It must
    resolve current C03 authority and return True only after independent approval,
    permission, expiry and exact binding checks. No adapter means fail closed.
    """
    _authorize(user)
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    if session.requester_id == user.pk:
        raise PermissionDenied
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        raise CountConflictError('Invalid command identity')
    binding = _binding(session, tenant_id, environment, policy_version)
    command_payload = {'kind': 'commit', 'binding': binding}
    if session.state == 'COMMITTED':
        if (
            session.committed_command == command_id
            and session.commands.get(command_id) == command_payload
        ):
            return session
        raise CountConflictError('Count already committed')
    if command_id in session.commands:
        raise CountConflictError('Command payload conflict')
    if session.state != 'REVIEW' or len(session.observations) != len(session.scope):
        raise CountConflictError('Every frozen item requires an observation')
    if approval_validator is None or not session.approval_request:
        raise CountConflictError('Authoritative approval dependency unavailable')
    items = list(
        StockItem.objects.select_for_update()
        .filter(pk__in=[row['item'] for row in session.scope])
        .order_by('pk')
    )
    if len(items) != len(session.scope):
        raise CountConflictError('Frozen stock no longer exists; recount required')
    by_id = {str(item.pk): item for item in items}
    targets = []
    for row in session.scope:
        item = by_id[row['item']]
        if item.serialized or not item.in_stock:
            raise CountConflictError('Stock eligibility changed; recount required')
        result = variance(row, session.observations[row['item']], _snapshot(item))
        targets.append((item, result['counted']))
    if (
        approval_validator(session.approval_request, binding, user, 'stocktake')
        is not True
    ):
        raise CountConflictError('Count is not independently approved')
    for item, count in targets:
        item.stocktake(
            count, user, notes=f'Cycle count {session.pk}, command {command_id}'
        )
    session.state = 'COMMITTED'
    session.committed_command = command_id
    session.commands[command_id] = command_payload
    session.save()
    return session
