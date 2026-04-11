# django-pgconstraints

[![PyPI version](https://img.shields.io/pypi/v/django-pgconstraints.svg?style=flat)](https://pypi.org/project/django-pgconstraints/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-pgconstraints.svg)](https://pypi.org/project/django-pgconstraints/)
[![CI](https://github.com/oliverhaas/django-pgconstraints/actions/workflows/ci.yml/badge.svg)](https://github.com/oliverhaas/django-pgconstraints/actions/workflows/ci.yml)

Declarative PostgreSQL constraint triggers for Django, with foreign-key
traversal that the built-in constraints can't express.

`UniqueConstraint`, `CheckConstraint`, and `GeneratedField` in Django 6.0
compile down to plain PostgreSQL constraints — so they can only reference
columns on the same table. `django-pgconstraints` provides three trigger-based
classes that accept `F("related__field")` expressions and compile to PL/pgSQL
triggers via [django-pgtrigger](https://github.com/Opus10/django-pgtrigger).
Their API mirrors the Django equivalents to keep the mental model familiar.

```python
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

## What's in the box

- **`UniqueConstraintTrigger`** — unique constraint whose `fields=`
  accepts FK chains (`"book__author"`). Also supports Django expressions,
  partial conditions, `nulls_distinct`, and deferred timing.
- **`CheckConstraintTrigger`** — check constraint whose `Q` can cross
  foreign keys (`Q(quantity__lte=F("product__stock"))`).
- **`GeneratedFieldTrigger`** — a computed column whose expression can
  reference related rows. The package automatically installs reverse
  triggers so the value stays in sync when that related data changes.

Each trigger also integrates with Django's `Model.full_clean()` so ORM-level
validation still catches violations before they hit the database, and each
accepts `violation_error_code` / `violation_error_message` so the errors
raised by `full_clean()` match your application's error-handling conventions.

## Install

```bash
pip install django-pgconstraints
# or
uv add django-pgconstraints
```

Add `pgtrigger` and `django_pgconstraints` to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "pgtrigger",
    "django_pgconstraints",
    ...
]
```

Triggers go in `Meta.triggers` (a django-pgtrigger extension), **not**
`Meta.constraints`. The package ships a system check
(`pgconstraints.E001`) that flags the common mistake.

## Requirements

- Python 3.14+
- Django 6.0
- PostgreSQL (any currently supported version)
- django-pgtrigger 4.17+

## Documentation

Full documentation at [oliverhaas.github.io/django-pgconstraints](https://oliverhaas.github.io/django-pgconstraints/).

Runnable examples live in [`examples/`](examples/):

- [`examples/simple/`](examples/simple/) — cross-publisher unique chapter names
- [`examples/full/`](examples/full/) — inventory + purchase orders exercising all three triggers

## License

MIT
