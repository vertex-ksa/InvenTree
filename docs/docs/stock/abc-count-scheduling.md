# ABC count scheduling proposal

The opt-in cycle-count API includes `GET /api/stock/cycle-count/abc/<location>/`.
It requires the currently assigned native reviewer, current stock read/change
and approval permissions, active user and ownership of every current stock unit.
Blind counters cannot read the economic schedule. This endpoint creates no count,
approval, movement, task or posting.

Configure `count_abc_policies` in the existing server configuration. Each location
key selects a complete proposal policy and exact annual usage values keyed by
native part ID. Values must be reconciled in the declared currency and period;
the server configuration is operator supplied evidence, not an automated native
annual usage calculation. No currency conversion or policy defaults are inferred.

```yaml
count_abc_policies:
  "101":
    policy:
      version: reviewed-policy-1
      currency: SAR
      period: "2026"
      aShare: "0.80"
      bShare: "0.95"
      intervalDays: {A: 30, B: 90, C: 365}
    annualUsageValues:
      "201": "1000.00000001"
```

The value keys must exactly match the native parts currently at that location.
Equal-value groups share a class; a group crossing a cumulative threshold retains
the class at its starting share. Zero-value materials are class C. Each part's
last count is the oldest last STOCK_COUNT entry among its current stock units.
Any unit with no count history makes the part due today. A stable digest binds
the exact policy, as-of date and resulting material proposal. Native monetary
source reconciliation, persisted governed policy editing and automatic task
creation remain separate work; this proposal grants no adjustment authority.

Run `manage.py test stock.test_counting_api stock.test_counting_abc
stock.test_counting_policy` for current actor, ownership, exact arithmetic and
schedule regressions. Existing native count approval and commit remain separate.
