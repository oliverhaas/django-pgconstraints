# UniqueConstraintTrigger

A trigger-based unique constraint. The API mirrors Django's
`UniqueConstraint` with the same parameter names and semantics where they
overlap, except `fields=` additionally accepts foreign-key chains
(`"book__author"`).

```python
from django_pgconstraints import UniqueConstraintTrigger
```

## Signature

```python
UniqueConstraintTrigger(
    *expressions: BaseExpression,
    fields: list[str] | tuple[str, ...] = (),
    condition: Q | None = None,
    deferrable: Deferrable | None = None,
    nulls_distinct: bool | None = None,
    index: bool = False,
    violation_error_code: str | None = None,
    violation_error_message: str | None = None,
    name: str,
)
```

At least one of `fields` or `expressions` must be provided, and the two
are mutually exclusive.

## Plain field uniqueness

```python
class Page(models.Model):
    slug = models.SlugField()
    section = models.CharField(max_length=50)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["slug", "section"],
                name="page_unique_slug_per_section",
            ),
        ]
```

## Foreign-key traversal

`fields=` accepts `__`-separated chains. Each hop becomes a subquery in
the generated trigger, so the uniqueness key can live several tables
away:

```python
UniqueConstraintTrigger(
    fields=["name", "series__publisher"],
    name="chapter_unique_name_per_publisher",
)
```

This is the main reason to reach for `UniqueConstraintTrigger` over
Django's built-in `UniqueConstraint`, which can only reference columns on
the current table.

## Expressions

Pass Django expressions positionally instead of `fields=`:

```python
from django.db.models.functions import Lower

UniqueConstraintTrigger(
    Lower("email"),
    name="user_unique_email_ci",
)
```

`fields` and expressions cannot be combined on the same trigger. Triggers
built from expressions also cannot be deferred. PostgreSQL can't defer a
purely functional check the same way it defers a real constraint.

## Partial uniqueness

Supply a `Q` as `condition=` to only enforce uniqueness for matching rows:

```python
UniqueConstraintTrigger(
    fields=["slug"],
    condition=Q(status="published"),
    name="article_unique_slug_when_published",
)
```

## `nulls_distinct`

Matches PostgreSQL semantics:

- `nulls_distinct=None` (the default) — NULLs are treated as distinct, so
  multiple NULL values do not conflict.
- `nulls_distinct=False` — NULLs are **not** distinct; two rows with NULL
  in the same field collide.

The Python-level `validate()` skips the uniqueness check when any field
value is NULL unless `nulls_distinct=False`, so `full_clean()` sees the
same behaviour as the database.

## Deferred triggers

```python
from django.db.models import Deferrable

UniqueConstraintTrigger(
    fields=["position"],
    deferrable=Deferrable.DEFERRED,
    name="row_unique_position",
)
```

A deferred trigger fires at transaction commit, which means you can
freely swap values mid-transaction (`a.position, b.position = b.position,
a.position`) as long as the final state is valid. Deferred triggers are
not supported for expression-based triggers.

## Error messages

Both `violation_error_code` and `violation_error_message` are plumbed
through to Django's `ValidationError` when `validate()` runs:

```python
UniqueConstraintTrigger(
    fields=["slug"],
    violation_error_code="slug_taken",
    violation_error_message="That slug is already in use.",
    name="article_unique_slug",
)
```

The database-level error still comes back as `IntegrityError` with
PostgreSQL error code `23505`.

## Index backing

By default, uniqueness is enforced solely by the trigger. Pass
`index=True` to also create a matching `CREATE UNIQUE INDEX`:

```python
UniqueConstraintTrigger(
    fields=["slug", "section"],
    index=True,
    name="page_unique_slug_per_section",
)
```

The index makes uniqueness checks O(log n) per insert instead of the
trigger's O(n) `EXISTS` scan, and lets the query planner use the column
set for ordinary `SELECT` queries. The trigger stays installed as a
second layer of defense and for `validate()` / `full_clean()` support.

Supported with `index=True`:

- Plain fields, composite fields
- Expressions (`Lower("email")`)
- Partial conditions (`condition=Q(published=True)`)
- `nulls_distinct=False` (PostgreSQL 15+)

Not supported with `index=True` (raises `ValueError`):

- FK-traversal `__` in `fields` — unique indexes only cover same-table
  columns
- FK-traversal `F()` references in expressions
- `deferrable=Deferrable.DEFERRED` — PostgreSQL unique indexes cannot be
  deferred

**Trade-off:** when `index=True` rejects a duplicate, the error comes
from PostgreSQL's native index-level message, not the trigger's
`violation_error_message`. If you need the custom error message, use
`index=False` (the default) and rely on the trigger alone.

## Concurrency

The trigger takes an advisory transaction lock keyed on the hash of the
uniqueness value before doing the existence check, so two concurrent
inserts of the same value on different sessions cannot both succeed by
racing each other. This is the same technique
[`django-pgtrigger` recommends](https://django-pgtrigger.readthedocs.io/)
for trigger-based uniqueness.
