"""Cycle-count policy primitives; stock mutations remain native stock operations.

The first policy deliberately requires a recount after ANY intervening movement.
It never guesses a movement's timing relative to a blind physical observation.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation


class CountConflictError(ValueError):
    """An observation cannot safely become a physical-stock adjustment."""


def quantity_text(value):
    """Validate exact native five-place quantity input without float coercion."""
    if not isinstance(value, str):
        raise CountConflictError('Quantity must be an exact decimal string')
    try:
        quantity = Decimal(value)
    except InvalidOperation as error:
        raise CountConflictError('Invalid quantity') from error
    if not quantity.is_finite() or quantity < 0 or quantity >= Decimal('1e10'):
        raise CountConflictError('Quantity is outside native limits')
    if quantity != quantity.quantize(Decimal('0.00001')):
        raise CountConflictError('Quantity exceeds native precision')
    return format(quantity, '.5f')


def evidence_hash(snapshot):
    """Hash the restricted C03/1 JSON subset (ASCII field names, string values)."""

    def validate(value):
        if isinstance(value, str):
            return
        if isinstance(value, list):
            for item in value:
                validate(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not key.isascii():
                    raise CountConflictError('Evidence keys must be ASCII strings')
                validate(item)
            return
        raise CountConflictError(
            'Evidence requires exact string quantities and identifiers'
        )

    validate(snapshot)
    payload = json.dumps(
        snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def variance(scope, observation, current):
    """Reject stale stock before deriving a native absolute stocktake target."""
    for field in ('item', 'location', 'quantity', 'tracking'):
        if current[field] != scope[field]:
            raise CountConflictError('Stock changed during count; recount required')
    for field in ('part', 'partName', 'partIPN', 'partRevision', 'batch', 'unit'):
        if field in scope and current.get(field) != scope[field]:
            raise CountConflictError('Stock identity changed; recount required')
    count = quantity_text(observation)
    return {
        'item': scope['item'],
        'expected': scope['quantity'],
        'counted': count,
        'delta': format(Decimal(count) - Decimal(scope['quantity']), '.5f'),
    }
