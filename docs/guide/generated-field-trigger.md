# GeneratedFieldTrigger

A trigger-driven computed column whose expression can reference related
rows. When a foreign-key chain appears in the expression, the package
also installs reverse triggers on those related models so the value
stays in sync when the referenced data changes.

```python
from django_pgconstraints import GeneratedFieldTrigger
```

## Signature

```python
GeneratedFieldTrigger(
    *,
    field: str,
    expression: BaseExpression,
    auto_refresh: bool = True,
    name: str,
)
```

## How it differs from `GeneratedField`

| | `GeneratedField` | `GeneratedFieldTrigger` |
| --- | --- | --- |
| Storage | PostgreSQL `GENERATED ALWAYS AS … STORED` | Regular column kept in sync by a `BEFORE INSERT OR UPDATE` trigger |
| FK traversal in the expression | Not supported | Supported (`F("product__price")`) |
| Target column definition | Declared implicitly | Must be defined manually on the model |
| External writes | Rejected by PostgreSQL | Silently overwritten on next write |

The target column is a regular field, which means you define it like any
other field. Pick a type and a default that is valid until the trigger
runs.

## Simple expression (same table)

```python
from django.db.models import F

class LineItem(models.Model):
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField()
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="total",
                expression=F("price") * F("quantity"),
                name="lineitem_total",
            ),
        ]
```

By default (`auto_refresh=True`), `save()` and `bulk_create()` piggyback
a `RETURNING` clause on the statement Django already issues, so the
computed value is written back onto the Python instance with no extra
round-trip. See [Instance refresh](#instance-refresh) below.

## Foreign-key traversal and reverse triggers

```python
class Supplier(models.Model):
    markup_pct = models.IntegerField(default=10)


class Part(models.Model):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    markup_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        triggers = [
            GeneratedFieldTrigger(
                field="markup_amount",
                expression=F("base_price") * F("supplier__markup_pct") / 100,
                name="part_markup_amount",
            ),
        ]
```

On every `INSERT` or `UPDATE` of a `Part` the forward trigger recomputes
`markup_amount` from the current `Supplier.markup_pct`.

In addition, the package installs a **reverse trigger** on `Supplier`:
whenever `Supplier.markup_pct` changes, every related `Part` row has its
`markup_amount` recomputed in the same transaction. Reverse triggers are
registered from `AppConfig.ready()` and follow arbitrary-depth FK chains.
If the expression is `F("part__supplier__markup_pct")`, reverse triggers
are installed on both `Part` (for changes to `Part.supplier_id`) and
`Supplier` (for changes to `markup_pct`).

## Read-only by convention

The target column is a regular column; PostgreSQL will let you write to
it. Any manual write (ORM or raw SQL) is silently overwritten the next
time the row is written again, because the trigger reruns and replaces
the column value. Treat the field as read-only.

## Instance refresh

With `auto_refresh=True` (the default) the Python instance is kept in
sync with the trigger-computed value for the APIs that already round-trip
per object:

```python
item = LineItem(price=10, quantity=3)
item.save()
item.total  # Decimal('30.00'), arrived via RETURNING, no extra SELECT
```

| API                                | Refreshed automatically? | Mechanism |
| ---                                | ---                      | --- |
| `Model.save()` — INSERT            | Yes                      | `INSERT … RETURNING` |
| `Model.save()` — UPDATE            | Yes                      | `UPDATE … RETURNING` (single statement) |
| `Manager.bulk_create(objs)`        | Yes                      | `INSERT … RETURNING` per batch |
| `Manager.bulk_create(objs, update_conflicts=True, …)` | Yes   | `INSERT … ON CONFLICT DO UPDATE … RETURNING` per batch (upserts) |
| `Manager.bulk_update(objs, fields)`| **No**                   | See [bulk_update limitation](#bulk_update-limitation) |
| `QuerySet.update(**kwargs)`        | **No** (no instance)     | Call `refresh_from_db()` or re-query as needed |

Pass `auto_refresh=False` to skip the RETURNING wiring for a specific
trigger. This is useful if you never read the computed field from the
Python instance after writing, or if a third-party layer interferes with
the extended `returning_fields`. With the opt-out you stay on the
pre-auto-refresh behavior: the DB has the correct value, the in-memory
instance does not, and you call `refresh_from_db()` yourself.

### `bulk_update` limitation

`bulk_update` emits a `CASE WHEN … END` UPDATE that returns only a row
count, so the passed-in Python objects keep whatever values they held
before the call. This is a Django-wide limitation, not specific to
`GeneratedFieldTrigger`: Django's own `GeneratedField` has the same
staleness after `bulk_update`.

Django tracks this as [ticket #32406][t32406] (generalizing RETURNING to
`update()` / `bulk_update()`) with an active but unmerged draft
[PR #19298][pr19298]. The core-team blocker is API shape, not technical
doubt. The single-instance `save()` path was fixed earlier via
[ticket #27222][t27222], the same machinery this package piggybacks on.

Until `bulk_update` learns to return rows, the options are:

1. **Re-query**: after `bulk_update`, hit the database once more.
   ```python
   Model.objects.bulk_update(objs, ["price"])
   fresh = {o.pk: o for o in Model.objects.filter(pk__in=[o.pk for o in objs])}
   ```
2. **Use `bulk_create(update_conflicts=True)` as an upsert.** [Ticket
   #34698][t34698] (fixed in Django 5.0) made this path populate
   `RETURNING` fields, including trigger-backed values, onto the
   passed-in instances, on PostgreSQL / MariaDB 10.5+ / SQLite 3.35+.
   For workloads that already have full rows in Python, this is a
   zero-extra-query alternative to `bulk_update`:
   ```python
   Model.objects.bulk_create(
       objs,
       update_conflicts=True,
       update_fields=["price", "quantity"],
       unique_fields=["id"],
   )
   # objs now carry the trigger-computed values.
   ```
   Caveats:
   - **Full-instance payload.** `bulk_create` sends every column on
     every object, not just `update_fields`. `bulk_update` sends only
     the fields you list. If your objects were rehydrated from the DB
     you already have them; if you only know the PK and a delta, you'd
     have to load the rest first.
   - **Sequence bump on `AutoField` / `BigAutoField`.** PostgreSQL
     calls `nextval()` during the INSERT attempt before the conflict
     is detected, so using the upsert path as a pure update burns one
     sequence ID per row. Cosmetic for 64-bit sequences, but gaps
     accumulate.
   - **`BEFORE INSERT` triggers fire before the conflict resolves.**
     Then `BEFORE UPDATE` fires for the DO UPDATE branch. Plain
     `bulk_update` only fires `BEFORE UPDATE`. `GeneratedFieldTrigger`
     itself is unaffected (it fires on both and recomputes the same
     value), but other `BEFORE INSERT` triggers with side effects
     (auditing, ID minting, logging) will run on what is really an
     update.
   - **Performance.** For large batches of pure updates with few
     columns, the `CASE WHEN` path `bulk_update` uses is meaningfully
     cheaper than an INSERT-with-ON-CONFLICT. For small batches the
     difference is negligible.
3. **`refresh_from_db()` per instance** if the set is small.

[t32406]: https://code.djangoproject.com/ticket/32406
[pr19298]: https://github.com/django/django/pull/19298
[t27222]: https://code.djangoproject.com/ticket/27222
[t34698]: https://code.djangoproject.com/ticket/34698

## Reconciling after a trigger bypass

If triggers are bypassed (raw SQL, `ALTER TABLE ... DISABLE TRIGGER`,
restoring a dump), computed values go stale. Two tools reconcile them:

### `refresh_dependent(queryset)`

Recomputes every `GeneratedFieldTrigger` target that depends on the
queryset's model. Issues one `UPDATE ... SET col = col` per dependent
field, scoped to child rows that point at the queryset:

```python
from django_pgconstraints import refresh_dependent

# Only reconcile parts linked to these specific suppliers.
refresh_dependent(Supplier.objects.filter(pk__in=changed_ids))
```

No-ops when the queryset matches zero rows or no dependent triggers
exist. Safe to call inside a transaction.

### `refresh_computed_field` management command

Touches every row so the forward trigger recomputes the value:

```bash
# Refresh a specific field on a specific model.
python manage.py refresh_computed_field testapp.Part.markup_amount

# Refresh all GeneratedFieldTrigger fields on a model.
python manage.py refresh_computed_field testapp.Part

# Refresh every managed field in the project.
python manage.py refresh_computed_field --all
```

Use this after adding a new trigger to an existing table, or after
changing the expression of an existing trigger.

## Cycle detection

At startup, the package builds a dependency graph of all
`GeneratedFieldTrigger` expressions and checks for cycles. If trigger A
depends on a field that trigger B computes, and B depends on a field A
computes, a `CycleError` is raised immediately:

```
django_pgconstraints.cycles.CycleError:
    Computed field cycle detected: myapp.Model.a → myapp.Model.b → myapp.Model.a
```

`CycleError` is importable from the package root:

```python
from django_pgconstraints import CycleError
```

## Admin integration

`ComputedFieldsReadOnlyAdminMixin` automatically marks every
`GeneratedFieldTrigger` target field as read-only in the Django admin,
preventing users from typing into a field that would be silently
overwritten on save:

```python
from django.contrib import admin
from django_pgconstraints import ComputedFieldsReadOnlyAdminMixin

@admin.register(Part)
class PartAdmin(ComputedFieldsReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "base_price", "markup_amount")
```

The mixin preserves any `readonly_fields` you declare manually and
appends the computed fields on top.

## Validation

`GeneratedFieldTrigger` does not participate in `full_clean()`. It is
computing a value rather than enforcing a constraint. Whatever the trigger
produces is what ends up in the column.
