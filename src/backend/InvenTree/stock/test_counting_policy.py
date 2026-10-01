"""Dependency-free stock conservation oracles for the count policy."""

import unittest
from decimal import Decimal

from stock.counting import CountConflictError, evidence_hash, quantity_text, variance


class CountPolicyTests(unittest.TestCase):
    """Verify exact values, frozen movement cutoffs and stable binding."""

    def setUp(self):
        """Freeze one native stock scope."""
        self.scope = {
            'item': '12',
            'location': '3',
            'quantity': '10.00000',
            'tracking': '8',
        }

    def test_conservation(self):
        """The variance conserves exact stock quantities."""
        result = variance(self.scope, '8.5', dict(self.scope))
        self.assertEqual(
            Decimal(result['expected']) + Decimal(result['delta']), Decimal('8.5')
        )
        self.assertEqual(result['delta'], '-1.50000')

    def test_movement_and_round_trip_conflict(self):
        """A movement cannot disappear behind an unchanged balance."""
        for field, value in [
            ('quantity', '11.00000'),
            ('location', '4'),
            ('tracking', '9'),
        ]:
            current = dict(self.scope, **{field: value})
            with self.assertRaises(CountConflictError):
                variance(self.scope, '8', current)

    def test_exact_native_quantity_validation(self):
        """Reject nondecimal, negative and out-of-range quantities."""
        for invalid in [
            0.1,
            '-1',
            'NaN',
            'Infinity',
            '1.000001',
            '100000000000000',
            'garbage',
        ]:
            with self.assertRaises(CountConflictError):
                quantity_text(invalid)
        self.assertEqual(quantity_text('1e-5'), '0.00001')

    def test_binding_ignores_key_order_and_changes_with_evidence(self):
        """Bind approval to immutable evidence regardless of key order."""
        self.assertEqual(
            evidence_hash({'a': '1', 'b': '2'}), evidence_hash({'b': '2', 'a': '1'})
        )
        self.assertNotEqual(
            evidence_hash(self.scope),
            evidence_hash(dict(self.scope, quantity='9.00000')),
        )

    def test_binding_rejects_noncanonical_evidence(self):
        """Reject accidental number or Unicode-key drift from C03 subset."""
        for invalid in [{'quantity': 0.1}, {'كمية': '1'}, {'quantity': None}]:
            with self.assertRaises(CountConflictError):
                evidence_hash(invalid)
