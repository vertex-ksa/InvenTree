# Atomic shipped counters — partial STOCK-02 prerequisite

Parent/source: e80fa79291d2f77aa973108ea6949ce580e55173 on codex/tm50/inventory-capabilities-20261002. Separate selected-dispatch recovery ccdec586 remains preserved and was not merged.

complete_allocations aggregates Decimal shipped deltas per line and updates F('shipped') + delta in sorted line order. This avoids replacing current database totals with stale cached values when distinct shipments/stock share a line. Existing stock/permission/transaction logic is retained; no full WMS acceptance claimed.

Added SalesOrderShippedCounterConcurrencyTest.test_distinct_shipments_increment_shared_line: two worker connections and a barrier after both line snapshots load, exact fractional shared total, stock ownership/quantity and native tracking assertions. Requires real row-lock-capable database; SQLite sequential proof is insufficient.

Checks executed: original native Git blob identities, Python AST on both files, source whitespace and clean patch application. PostgreSQL concurrency test, affected Django suite, Ruff and required pre-commit remain NOT_EXECUTED because native dependencies/runtime are absent. Therefore SOURCE_ONLY / native acceptance pending, not FAST_GREEN, BATCH_GREEN or RELEASE_GREEN.

InvenTree AGENTS requires human manual review before opening a PR. No PR or AI issue opened. No browser/gateway/tenant/queue/production/deployment proof claimed; original assigned epics remain open.
