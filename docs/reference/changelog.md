# Changelog

## 0.2.0 (2026-04-23)

### Features

- `GeneratedFieldTrigger(auto_refresh=True)` (default) — `save()` and
  `bulk_create()` now populate the trigger-computed value onto the
  in-memory instance without an extra query, by piggybacking a
  `RETURNING` clause on the statement Django already issues.
  `bulk_create(update_conflicts=True)` upserts are also covered. Pass
  `auto_refresh=False` to opt out per trigger. `QuerySet.update()` and
  `bulk_update()` remain out of scope — see the guide for why.

## 0.1.0 (2026-04-12)

Initial release. Three trigger classes, plus tooling for computed-field
lifecycle management.

### Trigger classes

- `UniqueConstraintTrigger` — unique constraint with foreign-key
  traversal, Django expressions, partial conditions, `nulls_distinct`,
  deferred timing, and optional `index=True` backing.
- `CheckConstraintTrigger` — check constraint whose `Q` can reference
  columns on related models via `F("rel__field")`.
- `GeneratedFieldTrigger` — trigger-based generated field that supports
  foreign-key traversal in its expression and installs reverse triggers
  on related models so the value stays in sync. Reverse triggers are
  statement-level with transition tables for efficient bulk cascading.

### Computed-field tooling

- `refresh_dependent(queryset)` — recompute dependent computed fields
  after a trigger bypass (raw SQL, disabled triggers, dump restore).
- `refresh_computed_field` management command — touch rows to force
  recomputation of specific or all `GeneratedFieldTrigger` fields.
- `ComputedFieldsReadOnlyAdminMixin` — Django admin mixin that
  auto-marks computed fields as read-only.
- `CycleError` — raised at startup if `GeneratedFieldTrigger`
  dependencies form a cycle.

### Infrastructure

- Django system check `pgconstraints.E001` flags triggers accidentally
  placed in `Meta.constraints` instead of `Meta.triggers`.
