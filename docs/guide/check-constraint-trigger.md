# CheckConstraintTrigger

A trigger-based check constraint whose `Q` can reference columns on
related models via `F("rel__field")`. The API mirrors Django's
`CheckConstraint` so the parameters feel familiar.

```python
from django_pgconstraints import CheckConstraintTrigger
```

## Signature

```python
CheckConstraintTrigger(
    *,
    condition: Q,
    violation_error_code: str | None = None,
    violation_error_message: str | None = None,
    name: str,
)
```

The parameter name matches Django's `CheckConstraint`: `condition=`,
not `check=`.

## Local conditions

For same-table conditions it works exactly like `CheckConstraint`:

```python
class Product(models.Model):
    stock = models.IntegerField(default=0)

    class Meta:
        triggers = [
            CheckConstraintTrigger(
                condition=Q(stock__gte=0),
                name="product_stock_non_negative",
            ),
        ]
```

## Foreign-key traversal

Where `CheckConstraintTrigger` earns its keep — the right-hand side of a
lookup can live on a related model:

```python
class OrderLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField()

    class Meta:
        triggers = [
            CheckConstraintTrigger(
                condition=Q(quantity__lte=F("product__stock")),
                name="orderline_qty_lte_stock",
            ),
        ]
```

Each `__` hop in the `F()` reference compiles to a nested subquery, so
arbitrary-depth chains (`F("order__customer__tier")`) work the same way.

## Multiple checks on the same model

Each check is its own trigger — list as many as you need:

```python
class OrderLine(models.Model):
    class Meta:
        triggers = [
            CheckConstraintTrigger(
                condition=Q(quantity__gt=0),
                name="orderline_qty_positive",
            ),
            CheckConstraintTrigger(
                condition=Q(quantity__lte=F("product__stock")),
                name="orderline_qty_lte_stock",
            ),
        ]
```

## Validation

`CheckConstraintTrigger.validate()` participates in `full_clean()`:

- For a condition whose `F()` refs are all same-table, it falls through
  to `Q.check()` and raises `ValidationError` on the instance.
- For conditions that traverse foreign keys, Python-level evaluation is
  skipped — the trigger is the sole authority and the check happens at
  `save()` time.

Both paths honour `violation_error_code` and `violation_error_message`.
The message supports `%(name)s` interpolation for the trigger name:

```python
CheckConstraintTrigger(
    condition=Q(quantity__gt=0),
    violation_error_code="invalid_quantity",
    violation_error_message="%(name)s: quantity must be positive.",
    name="orderline_qty_positive",
)
```

## Database errors

Violations raise `IntegrityError` with PostgreSQL error code `23514`
(`check_violation`), same as a plain `CheckConstraint`.
