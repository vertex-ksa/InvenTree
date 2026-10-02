"""Exact, read-only ABC scheduling with explicitly supplied policy authority.

Inputs are annual usage values in one already reconciled currency and period.
This module does not fetch prices, elect currency conversion or move stock.
Equal-value materials share a class; a threshold-crossing group retains the
class at its starting cumulative share. Zero-value materials are class C.
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from stock.counting import CountConflictError, evidence_hash


def _decimal(value):
    if not isinstance(value, str) or len(value) > 64:
        raise CountConflictError('Usage values must be exact decimal strings')
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise CountConflictError('Invalid usage value') from error
    if not number.is_finite() or number < 0 or number > Decimal('1e18'):
        raise CountConflictError('Usage value is outside supported limits')
    if not -8 <= number.as_tuple().exponent <= 18:
        raise CountConflictError('Usage value exceeds supported precision')
    return number


def abc_count_schedule(materials, policy, as_of):
    """Return a policy-bound proposal, never a stock or count command.

    Callers must authorize current native access before obtaining inputs. The
    result contains economic values and must never enter a blind counter read.
    """
    with localcontext() as context:
        context.prec = 50
        return _abc_count_schedule(materials, policy, as_of)


def _abc_count_schedule(materials, policy, as_of):
    keys = {'version', 'currency', 'period', 'aShare', 'bShare', 'intervalDays'}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise CountConflictError('An explicit complete ABC policy is required')
    for key in ('version', 'currency', 'period'):
        if (
            not isinstance(policy[key], str)
            or not policy[key].strip()
            or len(policy[key]) > 128
        ):
            raise CountConflictError('Policy identity is required')
    a_share, b_share = _decimal(policy['aShare']), _decimal(policy['bShare'])
    if not Decimal('0') < a_share < b_share < Decimal('1'):
        raise CountConflictError('ABC shares must increase within zero and one')
    intervals = policy['intervalDays']
    if not isinstance(intervals, dict) or set(intervals) != {'A', 'B', 'C'}:
        raise CountConflictError('Every class requires an explicit count interval')
    if any(
        type(days) is not int or not 1 <= days <= 3660 for days in intervals.values()
    ):
        raise CountConflictError('Invalid count interval')
    if not isinstance(as_of, date) or type(as_of) is not date:
        raise CountConflictError('An exact schedule date is required')
    if not isinstance(materials, list) or len(materials) > 10000:
        raise CountConflictError('Schedule capacity exceeded')
    rows, seen = [], set()
    for material in materials:
        if not isinstance(material, dict) or set(material) != {
            'partId',
            'annualUsageValue',
            'lastCountDate',
        }:
            raise CountConflictError('Invalid material evidence')
        identifier = material['partId']
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 15
            or not identifier.isascii()
            or not identifier.isdigit()
            or str(int(identifier)) != identifier
            or int(identifier) <= 0
            or identifier in seen
        ):
            raise CountConflictError(
                'Material identities must be unique native identifiers'
            )
        seen.add(identifier)
        last = material['lastCountDate']
        if last is not None:
            try:
                parsed = date.fromisoformat(last)
            except (TypeError, ValueError) as error:
                raise CountConflictError('Invalid last count date') from error
            if parsed.isoformat() != last or parsed > as_of:
                raise CountConflictError('Last count date must precede schedule date')
        else:
            parsed = None
        rows.append((identifier, _decimal(material['annualUsageValue']), parsed))
    rows.sort(key=lambda row: (-row[1], int(row[0])))
    total = sum((row[1] for row in rows), Decimal('0'))
    cumulative, previous_value, classification = Decimal('0'), None, 'C'
    result = []
    for identifier, value, last in rows:
        if value != previous_value:
            classification = (
                'C'
                if not value
                else 'A'
                if cumulative < total * a_share
                else 'B'
                if cumulative < total * b_share
                else 'C'
            )
        previous_value = value
        cumulative += value
        try:
            due_date = (
                last + timedelta(days=intervals[classification]) if last else as_of
            )
        except OverflowError as error:
            raise CountConflictError('Count date exceeds supported calendar') from error
        result.append(
            {
                'partId': identifier,
                'class': classification,
                'annualUsageValue': format(value, 'f'),
                'nextCountDate': due_date.isoformat(),
                'due': 'YES' if due_date <= as_of else 'NO',
            }
        )
    binding = evidence_hash(
        {
            'domain': 'abc-count-schedule/1',
            'asOf': as_of.isoformat(),
            'policy': {
                **policy,
                'intervalDays': {key: str(value) for key, value in intervals.items()},
            },
            'materials': result,
        }
    )
    return {
        'policyVersion': policy['version'],
        'currency': policy['currency'],
        'period': policy['period'],
        'asOf': as_of.isoformat(),
        'totalUsageValue': format(total, 'f'),
        'binding': binding,
        'materials': result,
        'stockEffect': 'NONE',
    }
