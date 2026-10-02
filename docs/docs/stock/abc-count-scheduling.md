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

## Persisted manual-source policy governance

`POST /api/stock/cycle-count/abc-policy/` creates an immutable candidate with
`location_id`, `policy`, `annual_usage_values`, `source_reference`, and a retained
`command_id`. The planner needs current stock read/change plus the dedicated
native add-policy grant. The server selects the independent reviewer from the
existing installation policy; callers cannot choose reviewer or deployment.
The reviewer needs both count approval and the dedicated ABC-policy approval
grant, and cannot approve a proposal they created. Decisions use independent
`expected_revision`, `decision`, `reason`, and `command_id` at
`POST /api/stock/cycle-count/abc-policy/<id>/`. Revocation remains available.

Native material identity, units, revision, location and deployment policy bind
the proposal. Editing economic evidence requires a new candidate; a retained
identity with changed payload conflicts. Approval records manual source review.
The source remains explicitly `OPERATOR_INPUT_NOT_NATIVE_INGESTION`; there is no
automatic annual usage ingestion, provider qualification or stock authority.

`GET /api/stock/cycle-count/abc-policy/context/<location>/` returns current
permissioned planner/reviewer metadata and scoped policy selections.
`GET /api/stock/cycle-count/abc-policy/<id>/schedule/` requires current valid
APPROVED evidence and derives freshness from native per-unit count history.
It creates neither counts nor adjustments. Automatic task creation remains off.

Unknown outcomes are resolved through native reads:
`GET /api/stock/cycle-count/abc-policy/?command_id=<retained-id>` recognizes the
current planner's exact saved proposal identity. For decisions,
`GET /api/stock/cycle-count/abc-policy/<id>/?command_id=<retained-id>` returns the
recorded decision separately from current state. A previously recorded approval
can therefore be recognized while the current policy is REVOKED. These reads
never resubmit a command. Missing or denied lookup does not authorize dispatch.

Run `manage.py test stock.test_counting_api stock.test_counting_abc
stock.test_counting_policy` for current actor, ownership, exact arithmetic and
schedule regressions. Existing native count approval and commit remain separate.
