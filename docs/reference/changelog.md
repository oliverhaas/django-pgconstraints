# Changelog

## 0.1.0a1 (Unreleased)

Initial release. Three trigger classes:

- `UniqueConstraintTrigger` — unique constraint with foreign-key
  traversal, Django expressions, partial conditions, `nulls_distinct`,
  and deferred timing.
- `CheckConstraintTrigger` — check constraint whose `Q` can reference
  columns on related models via `F("rel__field")`.
- `GeneratedFieldTrigger` — trigger-based generated field that supports
  foreign-key traversal in its expression and installs reverse triggers
  on related models so the value stays in sync.

Also ships a Django system check (`pgconstraints.E001`) that flags
triggers accidentally placed in `Meta.constraints` instead of
`Meta.triggers`.
