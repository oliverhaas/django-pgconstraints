# django-pgconstraints

Declarative PostgreSQL constraint triggers for Django, with foreign-key
traversal that the built-in constraints can't express.

Django 6.0 ships `UniqueConstraint`, `CheckConstraint`, and `GeneratedField`,
and they compile down to plain PostgreSQL constraints, so they can only
reference columns on the same table. That rules out a handful of common
patterns:

- A `CheckConstraint` whose right-hand side lives on a related model
- A unique constraint that has to follow a foreign-key chain
- A generated column whose value depends on a foreign row

This package provides three trigger-based classes that accept
`F("related__field")` expressions and compile to PL/pgSQL triggers via
[django-pgtrigger](https://github.com/Opus10/django-pgtrigger). Their
APIs mirror the Django equivalents:

- [`UniqueConstraintTrigger`](guide/unique-constraint-trigger.md) — a
  unique constraint with FK chains, Django expressions, partial
  conditions, and deferred timing.
- [`CheckConstraintTrigger`](guide/check-constraint-trigger.md) — a
  check whose `Q` can cross foreign keys.
- [`GeneratedFieldTrigger`](guide/generated-field-trigger.md) — a
  computed column whose expression can reference related rows, with
  reverse triggers that keep the value in sync and `RETURNING` wiring
  so `save()` / `bulk_create()` populate the computed value onto the
  Python instance without a follow-up query.

## A quick taste

```python
from django.db import models
from django.db.models import F, Q
from django_pgconstraints import CheckConstraintTrigger

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

On every `INSERT` or `UPDATE` the trigger runs, resolves
`F("product__stock")` via a subquery, and raises `IntegrityError`
(error code `23514`) if the condition is violated. The same rule is also
enforced at Python level through `full_clean()`.

Next: [installation](getting-started/installation.md) or jump straight to
the [quickstart](getting-started/quickstart.md).
