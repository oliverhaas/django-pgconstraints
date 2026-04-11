# Installation

## Requirements

- Python 3.14+
- Django 6.0
- PostgreSQL (any currently supported version)
- [django-pgtrigger](https://github.com/Opus10/django-pgtrigger) 4.17+ (pulled in automatically)

## Install

```bash
pip install django-pgconstraints
```

or with uv:

```bash
uv add django-pgconstraints
```

## Register the app

Add `pgtrigger` and `django_pgconstraints` to `INSTALLED_APPS`. Both are
required: the trigger classes inherit from `pgtrigger.Trigger` and are
installed via django-pgtrigger's machinery.

```python
INSTALLED_APPS = [
    # ...
    "pgtrigger",
    "django_pgconstraints",
    # your apps
]
```

## Where triggers go

Triggers live in **`Meta.triggers`**, a hook contributed by
django-pgtrigger. They do **not** go in `Meta.constraints`:

```python
class OrderLine(models.Model):
    ...

    class Meta:
        triggers = [
            CheckConstraintTrigger(
                condition=Q(quantity__gt=0),
                name="orderline_qty_positive",
            ),
        ]
```

Putting them in `Meta.constraints` would silently do nothing at the database
level, so `django_pgconstraints` ships a system check (`pgconstraints.E001`)
that fails `manage.py check` if you make that mistake.

## Installing the triggers in the database

django-pgtrigger installs triggers as part of `migrate`. After adding or
changing a trigger, run:

```bash
python manage.py makemigrations
python manage.py migrate
```

See the [django-pgtrigger installation
docs](https://django-pgtrigger.readthedocs.io/en/stable/installation.html)
for the other installation strategies (`pgtrigger install`, schema-editor
mode, etc.).
