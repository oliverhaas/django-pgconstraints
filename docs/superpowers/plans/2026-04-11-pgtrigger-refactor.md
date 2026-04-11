# Refactor django-pgconstraints to use django-pgtrigger

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hand-rolled trigger SQL generation with `pgtrigger.Trigger` subclasses, moving from `Meta.constraints` (BaseConstraint) to `Meta.triggers` (pgtrigger).

**Architecture:** Each of our 5 constraint types becomes a `pgtrigger.Trigger` subclass that overrides `get_func()` to generate the PL/pgSQL body. pgtrigger handles function naming, CREATE OR REPLACE, migrations (AddTrigger/RemoveTrigger), identifier limits, and the ignore mechanism. Our `_sql_value`, `_q_to_sql`, `_check_q_to_sql`, and field-resolution helpers remain as internal SQL-generation utilities. Python-level `validate()` moves to standalone functions or mixin methods since pgtrigger triggers don't have a validate concept.

**Tech Stack:** django-pgtrigger>=4.17.0, Django>=5.2, PostgreSQL, psycopg

---

## What pgtrigger gives us for free (things we DELETE)

- `CREATE OR REPLACE FUNCTION` + `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` — handled by `compiler.UpsertTriggerSql`
- Function naming (`pgtrigger_{name}_{hash}`) with 63-byte limit — handled by `get_pgid()`
- Idempotent install/uninstall — `trigger.install()` / `trigger.uninstall()`
- Migration auto-detection — `AddTrigger` / `RemoveTrigger` operations
- `DEFERRABLE INITIALLY DEFERRED` — `timing=pgtrigger.Deferred`
- The `_make_fn_name()` helper, `_PG_IDENT_MAX`, `_PREFIX` constants
- All `constraint_sql()`, `create_sql()`, `remove_sql()` methods
- All `deconstruct()` / `__eq__` / `__hash__` boilerplate

## What we KEEP

- `_sql_value()` — converts Python values to SQL literals
- `_q_to_sql()` — compiles Q objects to PL/pgSQL for single-table conditions
- `_check_q_to_sql()`, `_resolve_field_ref()`, `_resolve_f()`, `_build_comparison()` — FK-traversal SQL generation
- `_LOOKUP_OPS` — lookup-to-operator mapping
- Python-level validation logic (but restructured)

## File structure

```
django_pgconstraints/
    __init__.py                  # MODIFY: re-export new trigger classes
    triggers.py                  # CREATE: 5 pgtrigger.Trigger subclasses
    sql.py                       # CREATE: SQL helpers extracted from constraints.py
    validation.py                # CREATE: Python-side validate() functions
    constraints.py               # DELETE: replaced entirely
tests/
    settings/base.py             # MODIFY: add "pgtrigger" to INSTALLED_APPS
    testapp/models.py            # MODIFY: Meta.triggers instead of Meta.constraints
    test_cross_table_unique.py   # MODIFY: adapt lifecycle tests (no more constraint_sql)
    test_check_constraint_trigger.py  # MODIFY: same
    test_allowed_transitions.py  # MODIFY: same
    test_immutable.py            # MODIFY: same
    test_maintained_count.py     # MODIFY: same
    test_helpers.py              # MODIFY: update imports from sql.py
```

---

### Task 1: Extract SQL helpers to `sql.py`

**Files:**
- Create: `django_pgconstraints/sql.py`
- Modify: `django_pgconstraints/constraints.py` (verify nothing breaks)
- Test: `tests/test_helpers.py`

- [ ] **Step 1: Create `sql.py` with all helper functions**

Copy from `constraints.py` to `sql.py`:
- `_LOOKUP_OPS`
- `_sql_value()`
- `_q_to_sql()`
- `_resolve_field_ref()`
- `_resolve_f()`
- `_build_comparison()`
- `_check_q_to_sql()`

```python
"""SQL generation helpers for PL/pgSQL trigger bodies."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import FieldDoesNotExist
from django.db.models import F, Q

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model

_LOOKUP_OPS: dict[str, str] = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def _sql_value(value: str | int | float | bool | Decimal | date | datetime | timedelta | UUID | None) -> str:  # noqa: FBT001, PLR0911
    """Convert a Python value to a SQL literal for use in PL/pgSQL."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'::timestamptz"
    if isinstance(value, date):
        return f"'{value.isoformat()}'::date"
    if isinstance(value, timedelta):
        total = value.total_seconds()
        return f"'{total} seconds'::interval"
    if isinstance(value, UUID):
        return f"'{value}'::uuid"
    if value is None:
        return "NULL"
    msg = f"Cannot convert {type(value).__name__} to SQL literal"
    raise TypeError(msg)


def _q_to_sql(q: Q, model: type[Model], qn: Callable[[str], str], row_ref: str = "OLD") -> str:
    """Compile a simple Q object to PL/pgSQL using a row reference (OLD/NEW)."""
    parts: list[str] = []
    for child in q.children:
        if isinstance(child, Q):
            parts.append(f"({_q_to_sql(child, model, qn, row_ref)})")
        else:
            lookup_str, value = child  # type: ignore[misc]
            if "__" in lookup_str:
                field_name, lookup_type = lookup_str.rsplit("__", 1)
            else:
                field_name, lookup_type = lookup_str, "exact"

            column = model._meta.get_field(field_name).column  # type: ignore[union-attr]  # noqa: SLF001
            col = f"{row_ref}.{qn(column)}"

            if lookup_type in _LOOKUP_OPS:
                if lookup_type == "exact" and value is None:
                    parts.append(f"{col} IS NULL")
                else:
                    parts.append(f"{col} {_LOOKUP_OPS[lookup_type]} {_sql_value(value)}")
            elif lookup_type == "in":
                vals = ", ".join(_sql_value(v) for v in value)
                parts.append(f"{col} IN ({vals})")
            elif lookup_type == "isnull":
                parts.append(f"{col} IS NULL" if value else f"{col} IS NOT NULL")
            else:
                msg = f"Unsupported lookup type for trigger SQL: {lookup_type}"
                raise ValueError(msg)

    result = f" {q.connector} ".join(parts)
    if q.negated:
        result = f"NOT ({result})"
    return result


def _resolve_field_ref(
    chain: str,
    model: type[Model],
    qn: Callable[[str], str],
) -> tuple[str, str]:
    """Resolve a potentially cross-table field reference to SQL."""
    parts = chain.split("__")
    current_model = model
    fk_ref: str | None = None

    for i, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            lookup = "__".join(parts[i:])
            msg = f"No field resolved before lookup suffix '{lookup}' in '{chain}'"
            raise ValueError(msg) from None

        if field.is_relation:
            fk_column = field.column  # type: ignore[union-attr]
            related_model = field.related_model
            if fk_ref is None:
                fk_ref = f"NEW.{qn(fk_column)}"
            else:
                tbl = qn(current_model._meta.db_table)  # noqa: SLF001
                pk = qn(current_model._meta.pk.column)  # noqa: SLF001
                fk_ref = f"(SELECT {qn(fk_column)} FROM {tbl} WHERE {pk} = {fk_ref})"
            current_model = related_model  # type: ignore[assignment]
        else:
            col = qn(field.column)  # type: ignore[union-attr]
            if fk_ref is None:
                sql = f"NEW.{col}"
            else:
                tbl = qn(current_model._meta.db_table)  # noqa: SLF001
                pk = qn(current_model._meta.pk.column)  # noqa: SLF001
                sql = f"(SELECT {col} FROM {tbl} WHERE {pk} = {fk_ref})"
            lookup = "__".join(parts[i + 1 :]) or "exact"
            return sql, lookup

    msg = f"Field chain '{chain}' ends with a relation, expected a concrete field"
    raise ValueError(msg)


def _resolve_f(f_expr: F, model: type[Model], qn: Callable[[str], str]) -> str:
    """Resolve an F() expression to SQL."""
    name: str = f_expr.name  # type: ignore[attr-defined]
    sql, lookup = _resolve_field_ref(name, model, qn)
    if lookup != "exact":
        msg = f"F expression '{name}' should not contain a lookup suffix (got '{lookup}')"
        raise ValueError(msg)
    return sql


def _build_comparison(lhs_sql: str, lookup: str, rhs_value: object, rhs_sql: str) -> str:
    """Build a single SQL comparison clause."""
    if lookup in _LOOKUP_OPS:
        if lookup == "exact" and rhs_value is None:
            return f"{lhs_sql} IS NULL"
        return f"{lhs_sql} {_LOOKUP_OPS[lookup]} {rhs_sql}"
    if lookup == "in":
        if isinstance(rhs_value, (list, tuple)):
            vals = ", ".join(_sql_value(v) for v in rhs_value)
            return f"{lhs_sql} IN ({vals})"
        return f"{lhs_sql} IN ({rhs_sql})"
    if lookup == "isnull":
        return f"{lhs_sql} IS NULL" if rhs_value else f"{lhs_sql} IS NOT NULL"
    msg = f"Unsupported lookup: {lookup}"
    raise ValueError(msg)


def _check_q_to_sql(q: Q, model: type[Model], qn: Callable[[str], str]) -> str:
    """Compile a Q object to trigger SQL, supporting FK traversal and F()."""
    parts: list[str] = []
    for child in q.children:
        if isinstance(child, Q):
            parts.append(f"({_check_q_to_sql(child, model, qn)})")
        else:
            lhs_chain, rhs_value = child  # type: ignore[misc]
            lhs_sql, lookup = _resolve_field_ref(lhs_chain, model, qn)
            rhs_sql = _resolve_f(rhs_value, model, qn) if isinstance(rhs_value, F) else _sql_value(rhs_value)
            parts.append(_build_comparison(lhs_sql, lookup, rhs_value, rhs_sql))

    result = f" {q.connector} ".join(parts)
    if q.negated:
        result = f"NOT ({result})"
    return result
```

- [ ] **Step 2: Update test imports**

In `tests/test_helpers.py`, change:
```python
from django_pgconstraints.constraints import _make_fn_name, _sql_value
```
to:
```python
from django_pgconstraints.sql import _sql_value
```

Remove the `TestMakeFnName` test class entirely (pgtrigger handles naming).

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_helpers.py -v --no-cov`
Expected: All `TestSqlValue`, `TestAllowedTransitionsHash`, `TestImmutableValidation`, `TestImmutableHash` pass. `TestMakeFnName` gone.

- [ ] **Step 4: Commit**

```bash
git add django_pgconstraints/sql.py tests/test_helpers.py
git commit -m "refactor: extract SQL helpers to sql.py"
```

---

### Task 2: Create trigger classes in `triggers.py`

**Files:**
- Create: `django_pgconstraints/triggers.py`

Each trigger class extends `pgtrigger.Trigger` and overrides `get_func(model)` to return the PL/pgSQL body. pgtrigger handles everything else: naming, CREATE OR REPLACE, migrations, deferral.

- [ ] **Step 1: Create `triggers.py` with all 5 trigger classes**

```python
"""Trigger implementations using django-pgtrigger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pgtrigger
from django.db.models import F, Q

from django_pgconstraints.sql import (
    _check_q_to_sql,
    _q_to_sql,
    _sql_value,
)

if TYPE_CHECKING:
    from django.db import models


class UniqueAcross(pgtrigger.Trigger):
    """Enforce uniqueness of a field's value across two tables.

    Uses a deferrable constraint trigger that acquires an advisory lock
    then checks the other table on INSERT or UPDATE.  Raises SQLSTATE 23505.

    Each table in the pair needs its own ``UniqueAcross`` pointing at the
    other table.  Within-table uniqueness is **not** enforced — use Django's
    ``UniqueConstraint`` for that.
    """

    when = pgtrigger.After
    operation = pgtrigger.Insert | pgtrigger.Update
    timing = pgtrigger.Deferred

    def __init__(
        self,
        *,
        field: str,
        across: str,
        across_field: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.field = field
        self.across = across
        self.across_field = across_field or field
        super().__init__(**kwargs)

    def get_func(self, model: models.Model) -> str:
        from django.apps import apps

        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)

        app_label, model_name = self.across.split(".")
        across_model = apps.get_model(app_label, model_name)
        across_table = qn(across_model._meta.db_table)
        across_column = qn(across_model._meta.get_field(self.across_field).column)

        return self.format_sql(f"""
            IF NEW.{column} IS NOT NULL THEN
                PERFORM pg_advisory_xact_lock(hashtext(NEW.{column}::text));
                IF EXISTS (
                    SELECT 1 FROM {across_table}
                    WHERE {across_column} = NEW.{column}
                    FOR UPDATE
                ) THEN
                    RAISE EXCEPTION
                        'Cross-table unique constraint "%%s" is violated.', '{self.name}'
                    USING ERRCODE = '23505', CONSTRAINT = '{self.name}';
                END IF;
            END IF;
            RETURN NEW;
        """)


class CheckAcross(pgtrigger.Trigger):
    """A check constraint that can reference related tables via FK traversal.

    The ``check`` Q object may contain ``F()`` expressions that traverse
    foreign-key relationships (e.g. ``F("product__stock")``).
    Enforced by a BEFORE INSERT OR UPDATE trigger.  Raises SQLSTATE 23514.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    def __init__(self, *, check: Q, **kwargs: Any) -> None:
        self.check = check
        super().__init__(**kwargs)

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote
        check_sql = _check_q_to_sql(self.check, model, qn)

        return self.format_sql(f"""
            IF NOT ({check_sql}) THEN
                RAISE EXCEPTION
                    'Check constraint "%%s" is violated.', '{self.name}'
                USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


class AllowedTransitions(pgtrigger.Trigger):
    """Restrict a field to an explicit set of state transitions.

    Compares ``OLD.<field>`` to ``NEW.<field>`` and rejects any change
    not listed in *transitions*.  Inserts are not constrained.
    Raises SQLSTATE 23514.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Update

    def __init__(
        self,
        *,
        field: str,
        transitions: dict[str, list[str]],
        **kwargs: Any,
    ) -> None:
        self.field = field
        self.transitions = transitions
        super().__init__(**kwargs)

    def get_condition(self, model: models.Model) -> pgtrigger.Condition:
        column = pgtrigger.utils.quote(model._meta.get_field(self.field).column)
        return pgtrigger.Condition(f"OLD.{column} IS DISTINCT FROM NEW.{column}")

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)

        conditions: list[str] = []
        for from_state, to_states in self.transitions.items():
            to_vals = ", ".join(_sql_value(s) for s in to_states)
            conditions.append(
                f"(OLD.{column} IS NOT DISTINCT FROM {_sql_value(from_state)}"
                f" AND NEW.{column} IN ({to_vals}))"
            )
        allowed = " OR ".join(conditions) if conditions else "FALSE"

        return self.format_sql(f"""
            IF NOT ({allowed}) THEN
                RAISE EXCEPTION
                    'Transition constraint "%%s" is violated: %%s -> %%s is not allowed.',
                    '{self.name}', OLD.{column}::text, NEW.{column}::text
                USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


class Immutable(pgtrigger.Trigger):
    """Prevent changes to specific fields, optionally conditioned on row state.

    When *when_condition* is provided (a ``Q`` object), the fields are only
    immutable while the **OLD** row matches the condition.
    Raises SQLSTATE 23514.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Update

    def __init__(
        self,
        *,
        fields: list[str],
        when_condition: Q | None = None,
        **kwargs: Any,
    ) -> None:
        if not fields:
            msg = "Immutable trigger requires at least one field."
            raise ValueError(msg)
        self.fields = list(fields)
        self.when_condition = when_condition
        super().__init__(**kwargs)

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote

        changed_parts = []
        for field_name in self.fields:
            col = qn(model._meta.get_field(field_name).column)
            changed_parts.append(f"OLD.{col} IS DISTINCT FROM NEW.{col}")
        changed_check = " OR ".join(changed_parts)

        if self.when_condition is not None:
            when_sql = _q_to_sql(self.when_condition, model, qn, row_ref="OLD")
            full_check = f"({when_sql}) AND ({changed_check})"
        else:
            full_check = changed_check

        return self.format_sql(f"""
            IF {full_check} THEN
                RAISE EXCEPTION
                    'Immutability constraint "%%s" is violated.', '{self.name}'
                USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


class MaintainCount(pgtrigger.Trigger):
    """Keep a denormalised count field in sync.

    Declared on the **child** model (the one with the FK). This is a
    factory that produces 3 triggers (insert/delete/update) — use
    ``MaintainCount.triggers()`` to get all three for ``Meta.triggers``.
    """

    def __init__(
        self,
        *,
        target: str,
        target_field: str,
        fk_field: str,
        **kwargs: Any,
    ) -> None:
        self.target = target
        self.target_field = target_field
        self.fk_field = fk_field
        super().__init__(**kwargs)

    def _get_target_meta(self, model: models.Model) -> tuple[str, str, str]:
        from django.apps import apps

        qn = pgtrigger.utils.quote
        target_model = apps.get_model(*self.target.split("."))
        t_table = qn(target_model._meta.db_table)
        t_pk = qn(target_model._meta.pk.column)
        t_cnt = qn(target_model._meta.get_field(self.target_field).column)
        return t_table, t_pk, t_cnt

    @classmethod
    def triggers(
        cls,
        *,
        name: str,
        target: str,
        target_field: str,
        fk_field: str,
    ) -> list[pgtrigger.Trigger]:
        """Create all 3 triggers (insert, delete, update) for count maintenance."""
        return [
            _MaintainCountInsert(
                name=f"{name}_ins",
                target=target,
                target_field=target_field,
                fk_field=fk_field,
            ),
            _MaintainCountDelete(
                name=f"{name}_del",
                target=target,
                target_field=target_field,
                fk_field=fk_field,
            ),
            _MaintainCountUpdate(
                name=f"{name}_upd",
                target=target,
                target_field=target_field,
                fk_field=fk_field,
            ),
        ]


class _MaintainCountInsert(MaintainCount):
    when = pgtrigger.After
    operation = pgtrigger.Insert

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote
        fk_col = qn(model._meta.get_field(self.fk_field).column)
        t_table, t_pk, t_cnt = self._get_target_meta(model)

        return self.format_sql(f"""
            IF NEW.{fk_col} IS NOT NULL THEN
                UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1 WHERE {t_pk} = NEW.{fk_col};
            END IF;
            RETURN NEW;
        """)


class _MaintainCountDelete(MaintainCount):
    when = pgtrigger.After
    operation = pgtrigger.Delete

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote
        fk_col = qn(model._meta.get_field(self.fk_field).column)
        t_table, t_pk, t_cnt = self._get_target_meta(model)

        return self.format_sql(f"""
            IF OLD.{fk_col} IS NOT NULL THEN
                UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1 WHERE {t_pk} = OLD.{fk_col};
            END IF;
            RETURN OLD;
        """)


class _MaintainCountUpdate(MaintainCount):
    when = pgtrigger.After
    operation = pgtrigger.Update

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Override operation to UpdateOf(fk_field) — but we need the model
        # to resolve the column name. We'll set it in get_func via the
        # operation attribute. Actually, pgtrigger.UpdateOf takes field names
        # as column names, so we defer this.

    def get_func(self, model: models.Model) -> str:
        qn = pgtrigger.utils.quote
        fk_col = qn(model._meta.get_field(self.fk_field).column)
        t_table, t_pk, t_cnt = self._get_target_meta(model)

        return self.format_sql(f"""
            IF OLD.{fk_col} IS DISTINCT FROM NEW.{fk_col} THEN
                IF OLD.{fk_col} IS NOT NULL THEN
                    UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1 WHERE {t_pk} = OLD.{fk_col};
                END IF;
                IF NEW.{fk_col} IS NOT NULL THEN
                    UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1 WHERE {t_pk} = NEW.{fk_col};
                END IF;
            END IF;
            RETURN NEW;
        """)
```

- [ ] **Step 2: Verify file parses**

Run: `uv run python -c "from django_pgconstraints.triggers import UniqueAcross, CheckAcross, AllowedTransitions, Immutable, MaintainCount; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add django_pgconstraints/triggers.py
git commit -m "feat: add pgtrigger-based trigger classes"
```

---

### Task 3: Create `validation.py` for Python-side validation

**Files:**
- Create: `django_pgconstraints/validation.py`

These are standalone functions that models can call from `clean()` or `full_clean()`. They replicate what the old `validate()` methods did.

- [ ] **Step 1: Create `validation.py`**

```python
"""Python-level validation helpers for use in model.clean() or forms."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS

if TYPE_CHECKING:
    from django.db.models import Model, Q


def validate_unique_across(
    *,
    instance: Model,
    field: str,
    across: str,
    across_field: str | None = None,
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This value already exists in a related table.",
    error_code: str = "cross_table_unique",
) -> None:
    """Raise ValidationError if the field value exists in the across table."""
    from django.apps import apps

    across_field = across_field or field
    value = getattr(instance, field)
    if value is None:
        return
    across_model = apps.get_model(*across.split("."))
    if across_model.objects.using(using).filter(**{across_field: value}).exists():  # type: ignore[attr-defined]
        raise ValidationError(error_message, code=error_code)


def validate_allowed_transition(
    *,
    instance: Model,
    field: str,
    transitions: dict[str, list[str]],
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This state transition is not allowed.",
    error_code: str = "invalid_transition",
) -> None:
    """Raise ValidationError if the field change is not an allowed transition."""
    if instance.pk is None:
        return

    model = type(instance)
    new_value = getattr(instance, field)
    try:
        old_value = (
            model._default_manager.using(using)  # noqa: SLF001
            .values_list(field, flat=True)
            .get(pk=instance.pk)
        )
    except model.DoesNotExist:  # type: ignore[attr-defined]
        return

    if old_value == new_value:
        return

    allowed = transitions.get(old_value, [])
    if new_value not in allowed:
        raise ValidationError(error_message, code=error_code)


def validate_immutable(
    *,
    instance: Model,
    fields: list[str],
    when: Q | None = None,
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This field cannot be changed.",
    error_code: str = "immutable_field",
) -> None:
    """Raise ValidationError if immutable fields were changed."""
    if instance.pk is None:
        return

    model = type(instance)
    qs = model._default_manager.using(using).filter(pk=instance.pk)  # noqa: SLF001
    if when is not None:
        qs = qs.filter(when)
    try:
        old_values = qs.values(*fields).get()
    except model.DoesNotExist:  # type: ignore[attr-defined]
        return

    for field_name in fields:
        if old_values[field_name] != getattr(instance, field_name):
            raise ValidationError(error_message, code=error_code)
```

- [ ] **Step 2: Commit**

```bash
git add django_pgconstraints/validation.py
git commit -m "feat: add Python-side validation helpers"
```

---

### Task 4: Update `__init__.py` exports

**Files:**
- Modify: `django_pgconstraints/__init__.py`

- [ ] **Step 1: Replace exports**

```python
"""Declarative PostgreSQL constraint triggers for Django."""

from django_pgconstraints.triggers import (
    AllowedTransitions,
    CheckAcross,
    Immutable,
    MaintainCount,
    UniqueAcross,
)
from django_pgconstraints.validation import (
    validate_allowed_transition,
    validate_immutable,
    validate_unique_across,
)

__all__ = [
    "AllowedTransitions",
    "CheckAcross",
    "Immutable",
    "MaintainCount",
    "UniqueAcross",
    "validate_allowed_transition",
    "validate_immutable",
    "validate_unique_across",
]
```

- [ ] **Step 2: Commit**

```bash
git add django_pgconstraints/__init__.py
git commit -m "refactor: update public exports for pgtrigger-based API"
```

---

### Task 5: Update test settings and models

**Files:**
- Modify: `tests/settings/base.py`
- Modify: `tests/testapp/models.py`

- [ ] **Step 1: Add pgtrigger to INSTALLED_APPS**

In `tests/settings/base.py`:
```python
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "pgtrigger",
    "testapp",
]
```

- [ ] **Step 2: Rewrite models to use `Meta.triggers`**

```python
from django.db import models
from django.db.models import F, Q

from django_pgconstraints import (
    AllowedTransitions,
    CheckAcross,
    Immutable,
    MaintainCount,
    UniqueAcross,
)

# --- UniqueAcross models ---


class Page(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        triggers = [
            UniqueAcross(
                field="slug",
                across="testapp.Post",
                name="page_unique_slug_across_post",
            ),
        ]


class Post(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        triggers = [
            UniqueAcross(
                field="slug",
                across="testapp.Page",
                name="post_unique_slug_across_page",
            ),
        ]


# --- AllowedTransitions model ---


class Order(models.Model):
    status = models.CharField(max_length=20, default="draft")

    class Meta:
        triggers = [
            AllowedTransitions(
                field="status",
                transitions={
                    "draft": ["pending"],
                    "pending": ["shipped", "cancelled"],
                    "shipped": ["delivered"],
                },
                name="order_status_transitions",
            ),
        ]


# --- Immutable model ---


class Invoice(models.Model):
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="draft")

    class Meta:
        triggers = [
            Immutable(
                fields=["amount"],
                when_condition=Q(status="paid"),
                name="invoice_immutable_amount_when_paid",
            ),
        ]


# --- MaintainCount models ---


class Author(models.Model):
    name = models.CharField(max_length=100)
    book_count = models.IntegerField(default=0)


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)

    class Meta:
        triggers = [
            *MaintainCount.triggers(
                name="maintain_author_book_count",
                target="testapp.Author",
                target_field="book_count",
                fk_field="author",
            ),
        ]


# --- CheckAcross models ---


class Product(models.Model):
    name = models.CharField(max_length=100)
    stock = models.IntegerField(default=0)
    max_order_quantity = models.IntegerField(default=100)


class OrderLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField()

    class Meta:
        triggers = [
            CheckAcross(
                check=Q(quantity__lte=F("product__stock")),
                name="orderline_qty_lte_stock",
            ),
            CheckAcross(
                check=Q(quantity__gt=0),
                name="orderline_qty_positive",
            ),
        ]
```

- [ ] **Step 3: Commit**

```bash
git add tests/settings/base.py tests/testapp/models.py
git commit -m "refactor: migrate test models to Meta.triggers"
```

---

### Task 6: Update test files

**Files:**
- Modify: `tests/test_cross_table_unique.py`
- Modify: `tests/test_allowed_transitions.py`
- Modify: `tests/test_immutable.py`
- Modify: `tests/test_maintained_count.py`
- Modify: `tests/test_check_constraint_trigger.py`
- Modify: `tests/test_helpers.py`

The key changes across all test files:
1. Replace `constraint = Model._meta.constraints[0]` with pgtrigger's install/uninstall API
2. Lifecycle tests: use `pgtrigger.registered()` and `trigger.install()`/`trigger.uninstall()`
3. Validation tests: use `validate_*()` functions instead of `constraint.validate()`
4. Deconstruct tests: **delete** — pgtrigger handles serialization
5. Equality/hash tests: **delete** — pgtrigger handles identity

These are large changes. Each test file is rewritten below.

- [ ] **Step 1: Rewrite `test_cross_table_unique.py`**

```python
"""Tests for UniqueAcross trigger."""

import threading

import psycopg
import pgtrigger
import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from testapp.models import Page, Post

from django_pgconstraints import UniqueAcross, validate_unique_across


@pytest.mark.django_db(transaction=True)
class TestUniqueAcrossEnforcement:
    def test_insert_duplicate_across_tables(self):
        Page.objects.create(slug="hello")
        with pytest.raises(IntegrityError):
            Post.objects.create(slug="hello")

    def test_reverse_direction(self):
        Post.objects.create(slug="world")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="world")

    def test_different_values_allowed(self):
        Page.objects.create(slug="page-slug")
        Post.objects.create(slug="post-slug")

    def test_distinct_values_in_same_pair(self):
        Page.objects.create(slug="alpha")
        Post.objects.create(slug="beta")
        Page.objects.create(slug="gamma")
        Post.objects.create(slug="delta")

    def test_update_to_duplicate_blocked(self):
        Page.objects.create(slug="taken")
        post = Post.objects.create(slug="free")
        post.slug = "taken"
        with pytest.raises(IntegrityError):
            post.save()

    def test_same_value_in_same_table_uses_regular_unique(self):
        Page.objects.create(slug="dup")
        with pytest.raises(IntegrityError):
            Page.objects.create(slug="dup")

    def test_deferred_trigger_fires_at_commit(self):
        Page.objects.create(slug="deferred-test")
        with pytest.raises(IntegrityError), transaction.atomic():
            Post.objects.create(slug="deferred-test")


@pytest.mark.django_db(transaction=True)
class TestUniqueAcrossValidation:
    def test_validate_raises_on_duplicate(self):
        Page.objects.create(slug="existing")
        post = Post(slug="existing")
        with pytest.raises(ValidationError) as exc_info:
            validate_unique_across(
                instance=post,
                field="slug",
                across="testapp.Page",
            )
        assert exc_info.value.code == "cross_table_unique"

    def test_validate_passes_for_unique_value(self):
        Page.objects.create(slug="taken")
        post = Post(slug="available")
        validate_unique_across(
            instance=post,
            field="slug",
            across="testapp.Page",
        )

    def test_validate_skips_null(self):
        post = Post(slug=None)
        validate_unique_across(
            instance=post,
            field="slug",
            across="testapp.Page",
        )


@pytest.mark.django_db(transaction=True)
class TestUniqueAcrossLifecycle:
    def _trigger_exists(self, trigger_name_fragment, table_name):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{trigger_name_fragment}%", table_name],
            )
            return cursor.fetchone() is not None

    def test_triggers_created(self):
        assert self._trigger_exists("page_unique_slug_across_post", "testapp_page")
        assert self._trigger_exists("post_unique_slug_across_page", "testapp_post")

    def test_remove_and_recreate(self):
        trigger = Page._meta.triggers[0]
        trigger.uninstall(Page)
        assert not self._trigger_exists("page_unique_slug_across_post", "testapp_page")

        trigger.install(Page)
        assert self._trigger_exists("page_unique_slug_across_post", "testapp_page")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _raw_connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"] or 5432,
        autocommit=False,
    )


@pytest.mark.django_db(transaction=True)
class TestUniqueAcrossConcurrency:
    def test_concurrent_cross_table_insert(self):
        """Advisory lock serialises concurrent inserts — exactly one must succeed."""
        results: list[str | None] = [None, None]
        barrier = threading.Barrier(2, timeout=5)

        def do_insert(table: str, idx: int) -> None:
            conn = _raw_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"INSERT INTO testapp_{table} (slug) VALUES ('race-slug')")
                barrier.wait()
                conn.commit()
                results[idx] = "ok"
            except Exception as e:  # noqa: BLE001
                results[idx] = type(e).__name__
                conn.rollback()
            finally:
                conn.close()

        threads = [
            threading.Thread(target=do_insert, args=("page", 0)),
            threading.Thread(target=do_insert, args=("post", 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        ok_count = results.count("ok")
        assert ok_count == 1, f"Expected exactly 1 success but got {results}"
```

- [ ] **Step 2: Rewrite `test_allowed_transitions.py`**

```python
"""Tests for AllowedTransitions trigger."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from testapp.models import Order

from django_pgconstraints import validate_allowed_transition

TRANSITIONS = {
    "draft": ["pending"],
    "pending": ["shipped", "cancelled"],
    "shipped": ["delivered"],
}


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsEnforcement:
    def test_allowed_transition(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        order.save()

    def test_multi_step_transitions(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        order.save()
        order.status = "shipped"
        order.save()
        order.status = "delivered"
        order.save()

    def test_disallowed_transition(self):
        order = Order.objects.create(status="draft")
        order.status = "shipped"
        with pytest.raises(IntegrityError):
            order.save()

    def test_no_transition_from_terminal_state(self):
        order = Order.objects.create(status="delivered")
        order.status = "draft"
        with pytest.raises(IntegrityError):
            order.save()

    def test_same_value_no_op(self):
        order = Order.objects.create(status="pending")
        order.status = "pending"
        order.save()

    def test_insert_any_state(self):
        Order.objects.create(status="delivered")
        Order.objects.create(status="shipped")
        Order.objects.create(status="draft")


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsValidation:
    def test_validate_disallowed(self):
        order = Order.objects.create(status="draft")
        order.status = "shipped"
        with pytest.raises(ValidationError) as exc_info:
            validate_allowed_transition(
                instance=order,
                field="status",
                transitions=TRANSITIONS,
            )
        assert exc_info.value.code == "invalid_transition"

    def test_validate_allowed(self):
        order = Order.objects.create(status="draft")
        order.status = "pending"
        validate_allowed_transition(
            instance=order,
            field="status",
            transitions=TRANSITIONS,
        )

    def test_validate_new_instance(self):
        order = Order(status="shipped")
        validate_allowed_transition(
            instance=order,
            field="status",
            transitions=TRANSITIONS,
        )

    def test_validate_same_value(self):
        order = Order.objects.create(status="draft")
        order.status = "draft"
        validate_allowed_transition(
            instance=order,
            field="status",
            transitions=TRANSITIONS,
        )


@pytest.mark.django_db(transaction=True)
class TestAllowedTransitionsLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("order_status_transitions", "testapp_order")

    def test_remove_and_recreate(self):
        trigger = Order._meta.triggers[0]
        trigger.uninstall(Order)
        assert not self._trigger_exists("order_status_transitions", "testapp_order")

        trigger.install(Order)
        assert self._trigger_exists("order_status_transitions", "testapp_order")
```

- [ ] **Step 3: Rewrite `test_immutable.py`**

```python
"""Tests for Immutable trigger."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Q
from testapp.models import Invoice

from django_pgconstraints import Immutable, validate_immutable


@pytest.mark.django_db(transaction=True)
class TestImmutableEnforcement:
    def test_change_blocked_when_condition_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("200.00")
        with pytest.raises(IntegrityError):
            inv.save()

    def test_change_allowed_when_condition_not_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.amount = Decimal("200.00")
        inv.save()

    def test_unrelated_field_change_allowed(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.status = "refunded"
        inv.save()

    def test_transition_to_paid_with_amount_change(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.status = "paid"
        inv.amount = Decimal("150.00")
        inv.save()

    def test_no_change_no_violation(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("100.00")
        inv.save()


@pytest.mark.django_db(transaction=True)
class TestImmutableValidation:
    def test_validate_blocked(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="paid")
        inv.amount = Decimal("200.00")
        with pytest.raises(ValidationError) as exc_info:
            validate_immutable(
                instance=inv,
                fields=["amount"],
                when=Q(status="paid"),
            )
        assert exc_info.value.code == "immutable_field"

    def test_validate_allowed_condition_not_met(self):
        inv = Invoice.objects.create(amount=Decimal("100.00"), status="draft")
        inv.amount = Decimal("200.00")
        validate_immutable(
            instance=inv,
            fields=["amount"],
            when=Q(status="paid"),
        )

    def test_validate_new_instance(self):
        inv = Invoice(amount=Decimal("100.00"), status="paid")
        validate_immutable(
            instance=inv,
            fields=["amount"],
            when=Q(status="paid"),
        )


@pytest.mark.django_db(transaction=True)
class TestImmutableLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_trigger_created(self):
        assert self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")

    def test_remove_and_recreate(self):
        trigger = Invoice._meta.triggers[0]
        trigger.uninstall(Invoice)
        assert not self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")

        trigger.install(Invoice)
        assert self._trigger_exists("invoice_immutable_amount_when_paid", "testapp_invoice")


class TestImmutableInit:
    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="at least one field"):
            Immutable(fields=[], name="c")
```

- [ ] **Step 4: Rewrite `test_maintained_count.py`**

```python
"""Tests for MaintainCount triggers."""

import pytest
from django.db import connection
from testapp.models import Author, Book

from django_pgconstraints import MaintainCount


@pytest.mark.django_db(transaction=True)
class TestMaintainCountEnforcement:
    def test_insert_increments(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        author.refresh_from_db()
        assert author.book_count == 1

    def test_multiple_inserts(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        Book.objects.create(title="Book B", author=author)
        Book.objects.create(title="Book C", author=author)
        author.refresh_from_db()
        assert author.book_count == 3

    def test_delete_decrements(self):
        author = Author.objects.create(name="Alice")
        book = Book.objects.create(title="Book A", author=author)
        book.delete()
        author.refresh_from_db()
        assert author.book_count == 0

    def test_queryset_delete(self):
        author = Author.objects.create(name="Alice")
        Book.objects.create(title="Book A", author=author)
        Book.objects.create(title="Book B", author=author)
        Book.objects.filter(author=author).delete()
        author.refresh_from_db()
        assert author.book_count == 0

    def test_fk_update_adjusts_both(self):
        alice = Author.objects.create(name="Alice")
        bob = Author.objects.create(name="Bob")
        book = Book.objects.create(title="Book A", author=alice)
        alice.refresh_from_db()
        assert alice.book_count == 1

        book.author = bob
        book.save()
        alice.refresh_from_db()
        bob.refresh_from_db()
        assert alice.book_count == 0
        assert bob.book_count == 1

    def test_multiple_authors(self):
        alice = Author.objects.create(name="Alice")
        bob = Author.objects.create(name="Bob")
        Book.objects.create(title="A1", author=alice)
        Book.objects.create(title="A2", author=alice)
        Book.objects.create(title="B1", author=bob)
        alice.refresh_from_db()
        bob.refresh_from_db()
        assert alice.book_count == 2
        assert bob.book_count == 1

    def test_bulk_create(self):
        author = Author.objects.create(name="Alice")
        Book.objects.bulk_create(
            [
                Book(title="B1", author=author),
                Book(title="B2", author=author),
                Book(title="B3", author=author),
            ]
        )
        author.refresh_from_db()
        assert author.book_count == 3


@pytest.mark.django_db(transaction=True)
class TestMaintainCountLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_triggers_created(self):
        table = "testapp_book"
        assert self._trigger_exists("maintain_author_book_count_ins", table)
        assert self._trigger_exists("maintain_author_book_count_del", table)
        assert self._trigger_exists("maintain_author_book_count_upd", table)

    def test_remove_and_recreate(self):
        triggers = Book._meta.triggers
        table = "testapp_book"

        for trigger in triggers:
            trigger.uninstall(Book)
        assert not self._trigger_exists("maintain_author_book_count_ins", table)
        assert not self._trigger_exists("maintain_author_book_count_del", table)
        assert not self._trigger_exists("maintain_author_book_count_upd", table)

        for trigger in triggers:
            trigger.install(Book)
        assert self._trigger_exists("maintain_author_book_count_ins", table)
        assert self._trigger_exists("maintain_author_book_count_del", table)
        assert self._trigger_exists("maintain_author_book_count_upd", table)
```

- [ ] **Step 5: Rewrite `test_check_constraint_trigger.py`**

```python
"""Tests for CheckAcross trigger."""

import pytest
from django.db import IntegrityError, connection
from django.db.models import F, Q
from testapp.models import OrderLine, Product


@pytest.mark.django_db(transaction=True)
class TestCheckAcrossEnforcement:
    def test_insert_within_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=5)

    def test_insert_at_stock_limit(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=10)

    def test_insert_exceeding_stock(self):
        product = Product.objects.create(name="Widget", stock=5)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=6)

    def test_update_to_exceed_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 11
        with pytest.raises(IntegrityError):
            line.save()

    def test_update_within_stock(self):
        product = Product.objects.create(name="Widget", stock=10)
        line = OrderLine.objects.create(product=product, quantity=5)
        line.quantity = 8
        line.save()

    def test_local_only_check(self):
        product = Product.objects.create(name="Widget", stock=10)
        with pytest.raises(IntegrityError):
            OrderLine.objects.create(product=product, quantity=0)

    def test_local_check_passes(self):
        product = Product.objects.create(name="Widget", stock=10)
        OrderLine.objects.create(product=product, quantity=1)

    def test_reassign_product(self):
        big = Product.objects.create(name="Big", stock=100)
        small = Product.objects.create(name="Small", stock=2)
        line = OrderLine.objects.create(product=big, quantity=50)
        line.product = small
        with pytest.raises(IntegrityError):
            line.save()


@pytest.mark.django_db(transaction=True)
class TestCheckAcrossLifecycle:
    def _trigger_exists(self, name_fragment, table):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname LIKE %s AND c.relname = %s",
                [f"%{name_fragment}%", table],
            )
            return cur.fetchone() is not None

    def test_triggers_created(self):
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
        assert self._trigger_exists("orderline_qty_positive", "testapp_orderline")

    def test_remove_and_recreate(self):
        trigger = OrderLine._meta.triggers[0]
        trigger.uninstall(OrderLine)
        assert not self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")

        trigger.install(OrderLine)
        assert self._trigger_exists("orderline_qty_lte_stock", "testapp_orderline")
```

- [ ] **Step 6: Update `test_helpers.py`**

Update imports and remove `TestMakeFnName`:
```python
from django_pgconstraints.sql import _sql_value
```

Remove `_make_fn_name` import and `TestMakeFnName` class. Keep `TestSqlValue`, `TestAllowedTransitionsHash` (update if hash behavior changed), `TestImmutableValidation`, `TestImmutableHash`.

For `TestAllowedTransitionsHash` — pgtrigger `Trigger` objects don't need custom `__hash__`. The test verifies our trigger classes can be used normally. Replace with a basic construction test if needed.

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest tests/ -x -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add tests/
git commit -m "test: rewrite tests for pgtrigger-based API"
```

---

### Task 7: Delete `constraints.py` and clean up

**Files:**
- Delete: `django_pgconstraints/constraints.py`

- [ ] **Step 1: Delete the old module**

```bash
rm django_pgconstraints/constraints.py
```

- [ ] **Step 2: Run full suite to verify nothing references it**

Run: `uv run pytest tests/ -x -v`
Expected: All pass

- [ ] **Step 3: Run linting and mypy**

Run: `uv run ruff check django_pgconstraints/ tests/ && uv run mypy django_pgconstraints/`
Expected: Clean

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove old constraints.py, migration to pgtrigger complete"
```

---

### Task 8: Run flake-finder stability test

- [ ] **Step 1: Run 50x flake check**

Run: `uv run pytest tests/ --flake-finder --flake-runs=50 --no-cov -q`
Expected: All pass, 0 failures

- [ ] **Step 2: Push and update PR**

```bash
git push
```
