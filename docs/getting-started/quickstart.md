# Quickstart

This walks through the smallest possible use case: a `Chapter` model whose
name must be unique per publisher, following two foreign keys.

## Models

```python
from django.db import models

from django_pgconstraints import UniqueConstraintTrigger


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Series(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(Publisher, on_delete=models.CASCADE)


class Chapter(models.Model):
    name = models.CharField(max_length=200)
    series = models.ForeignKey(Series, on_delete=models.CASCADE)

    class Meta:
        triggers = [
            UniqueConstraintTrigger(
                fields=["name", "series__publisher"],
                name="chapter_unique_name_per_publisher",
            ),
        ]
```

The key bit is `"series__publisher"` — two underscore-separated hops
from `Chapter` to `Publisher`. The trigger resolves it to a nested SQL
subquery under the hood, so the uniqueness check does not materialise the
join in Python.

## Migrate

```bash
python manage.py makemigrations
python manage.py migrate
```

django-pgtrigger creates a migration for the trigger and installs it into
PostgreSQL on `migrate`.

## Try it

```python
penguin = Publisher.objects.create(name="Penguin")
orbit = Publisher.objects.create(name="Orbit")

dune = Series.objects.create(title="Dune", publisher=penguin)
foundation = Series.objects.create(title="Foundation", publisher=penguin)
expanse = Series.objects.create(title="The Expanse", publisher=orbit)

Chapter.objects.create(name="Beginnings", series=dune)
Chapter.objects.create(name="Beginnings", series=expanse)     # OK — different publisher
Chapter.objects.create(name="Beginnings", series=foundation)  # IntegrityError
```

## What's next

- [`UniqueConstraintTrigger`](../guide/unique-constraint-trigger.md) — the
  full option surface: expressions, partial unique, `nulls_distinct`,
  deferrable, `validate()`.
- [`CheckConstraintTrigger`](../guide/check-constraint-trigger.md) — for
  arbitrary boolean conditions.
- [`GeneratedFieldTrigger`](../guide/generated-field-trigger.md) — for
  values that should be computed from other columns, including columns
  on related rows.
- [Validation](../guide/validation.md) — how the triggers hook into
  `Model.full_clean()`.
