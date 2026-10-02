"""Independent schedule oracles; no database or stock mutation is involved."""

import copy
import unittest
from datetime import date
from decimal import localcontext

from stock.counting import CountConflictError
from stock.counting_abc import abc_count_schedule


class ABCCountScheduleTests(unittest.TestCase):
    """Verify frozen policy identity and independent exact schedule outcomes."""

    def setUp(self):
        """Select an explicit synthetic policy without production defaults."""
        self.policy = {
            'version': 'approved-1',
            'currency': 'SAR',
            'period': '2026',
            'aShare': '0.8',
            'bShare': '0.95',
            'intervalDays': {'A': 30, 'B': 90, 'C': 365},
        }
        self.today = date(2026, 10, 2)

    def row(self, identifier, value, last=None):
        """Build exact material evidence."""
        return {'partId': identifier, 'annualUsageValue': value, 'lastCountDate': last}

    def test_ties_crossing_threshold_stay_together_and_zero_is_c(self):
        """Keep identical economic values together independent of input order."""
        rows = [
            self.row('3', '10'),
            self.row('2', '40'),
            self.row('1', '40'),
            self.row('4', '10'),
            self.row('5', '0'),
        ]
        result = abc_count_schedule(rows, self.policy, self.today)
        self.assertEqual(
            [(row['partId'], row['class']) for row in result['materials']],
            [('1', 'A'), ('2', 'A'), ('3', 'B'), ('4', 'B'), ('5', 'C')],
        )
        self.assertEqual(result['totalUsageValue'], '100')
        self.assertEqual(result['stockEffect'], 'NONE')
        self.assertEqual(
            result, abc_count_schedule(list(reversed(rows)), self.policy, self.today)
        )

    def test_due_boundary_and_policy_binding(self):
        """Treat the exact due date as due and bind frequency changes."""
        rows = [self.row('1', '100', '2026-09-02')]
        result = abc_count_schedule(rows, self.policy, self.today)
        self.assertEqual(result['materials'][0]['nextCountDate'], '2026-10-02')
        self.assertEqual(result['materials'][0]['due'], 'YES')
        changed = copy.deepcopy(self.policy)
        changed['intervalDays']['A'] = 31
        later = abc_count_schedule(rows, changed, self.today)
        self.assertEqual(later['materials'][0]['due'], 'NO')
        self.assertNotEqual(result['binding'], later['binding'])

    def test_exact_arithmetic_independent_of_ambient_decimal_context(self):
        """Preserve the smallest supported contribution under low caller precision."""
        rows = [
            self.row('1', '999999999999999999.12345678'),
            self.row('2', '0.00000001'),
        ]
        with localcontext() as context:
            context.prec = 5
            result = abc_count_schedule(rows, self.policy, self.today)
        self.assertEqual(result['totalUsageValue'], '999999999999999999.12345679')

    def test_invalid_or_ambiguous_evidence_fails_closed(self):
        """Reject numeric coercion, ambiguous identities and future evidence."""
        for rows in [
            [self.row('1', 'NaN')],
            [self.row('1', '0e9999999')],
            [self.row('1', '0.000000001')],
            [self.row('01', '1')],
            [self.row('1', '1'), self.row('1', '2')],
            [self.row('1', '1', '2026-10-03')],
            [self.row('1', 0.1)],
        ]:
            with self.subTest(rows=rows), self.assertRaises(CountConflictError):
                abc_count_schedule(rows, self.policy, self.today)
        for replacement in [True, 0, 3661]:
            policy = copy.deepcopy(self.policy)
            policy['intervalDays']['A'] = replacement
            with self.assertRaises(CountConflictError):
                abc_count_schedule([], policy, self.today)

    def test_empty_and_zero_value_materials_have_no_implied_economic_rank(self):
        """Do not fabricate an economic rank from absent usage."""
        self.assertEqual(
            abc_count_schedule([], self.policy, self.today)['materials'], []
        )
        self.assertEqual(
            abc_count_schedule([self.row('1', '0')], self.policy, self.today)[
                'materials'
            ][0]['class'],
            'C',
        )
