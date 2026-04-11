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
other field — pick a type and a default that is valid until the trigger
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

After `save()`, call `instance.refresh_from_db()` to see the computed
value. The in-memory Python object is not updated by the trigger.

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
registered from `AppConfig.ready()` and follow arbitrary-depth FK chains
— if the expression is `F("part__supplier__markup_pct")`, reverse
triggers are installed on both `Part` (for changes to `Part.supplier_id`)
and `Supplier` (for changes to `markup_pct`).

## Read-only by convention

The target column is a regular column; PostgreSQL will happily let you
write to it. Any manual write — ORM or raw SQL — is silently overwritten
the next time the row is written again, because the trigger reruns and
replaces the column value. Treat the field as read-only.

Code that needs the computed value right after a save should call
`refresh_from_db()`:

```python
item = LineItem.objects.create(price=10, quantity=3)
item.total  # Decimal('0') — the Python instance has the default
item.refresh_from_db()
item.total  # Decimal('30.00')
```

## Validation

`GeneratedFieldTrigger` does not participate in `full_clean()` — it is
computing a value rather than enforcing a constraint. Whatever the trigger
produces is what ends up in the column.
