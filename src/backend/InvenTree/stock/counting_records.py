"""Controlled opt-in count read models; never serialize the raw frozen session."""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from stock.counting import CountConflictError
from stock.counting_service import (
    _authorize_observer,
    _ownership,
    _require_material_scope,
)
from stock.models import CycleCountSession, StockItem


@transaction.atomic
def observer_record(session_id, user):
    """Return only identifying task metadata and the requester's own observations."""
    user = _authorize_observer(user)
    session = CycleCountSession.objects.select_for_update().get(pk=session_id)
    if session.requester_id != user.pk:
        raise PermissionDenied
    _require_material_scope(session)
    items = list(
        StockItem.objects.filter(pk__in=[row['item'] for row in session.scope])
        .select_related('part')
        .order_by('pk')
    )
    if len(items) != len(session.scope):
        raise CountConflictError('Count scope is unavailable')
    _ownership(user, session.location, items)
    frozen = {row['item']: row for row in session.scope}
    return {
        'sessionId': session.pk,
        'state': session.state,
        'revision': session.revision,
        'location': {'id': session.location_id, 'name': session.location.name},
        'items': [
            {
                'id': item.pk,
                'partId': int(frozen[str(item.pk)]['part']),
                'partName': frozen[str(item.pk)]['partName'],
                'partIPN': frozen[str(item.pk)]['partIPN'],
                'partRevision': frozen[str(item.pk)]['partRevision'],
                'unit': frozen[str(item.pk)]['unit'],
                'batch': frozen[str(item.pk)]['batch'],
                'observed': session.observations.get(str(item.pk)),
            }
            for item in items
        ],
    }
