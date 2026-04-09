"""Constraint implementations backed by PostgreSQL triggers."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db.models import BaseConstraint, F, Q

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.models import Model

# PostgreSQL truncates identifiers to 63 bytes.  We need room for the
# ``pgc_fn_`` prefix (7 bytes) plus optional suffixes like ``_ins`` (4 bytes).
_PG_IDENT_MAX = 63
_PREFIX = "pgc_fn_"
_MAX_SUFFIX = 4  # longest suffix: "_ins", "_del", "_upd"


# ------------------------------------------------------------------
# SQL helpers
# ------------------------------------------------------------------

_LOOKUP_OPS: dict[str, str] = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def _make_fn_name(table: str, name: str, suffix: str = "") -> str:
    """Build a trigger function name scoped to *table*, within PG's 63-byte limit.

    The format is ``pgc_fn_{table}_{name}{suffix}``.  If that exceeds 63 bytes
    we replace the middle portion with a short hash to guarantee uniqueness.
    """
    candidate = f"{_PREFIX}{table}_{name}{suffix}"
    if len(candidate.encode()) <= _PG_IDENT_MAX:
        return candidate
    # Truncate deterministically using a hash of the full candidate.
    digest = hashlib.md5(candidate.encode()).hexdigest()[:8]  # noqa: S324
    budget = _PG_IDENT_MAX - len(_PREFIX) - len(suffix) - len(digest) - 1  # 1 for '_'
    base = f"{table}_{name}"[:budget]
    return f"{_PREFIX}{base}_{digest}{suffix}"


def _sql_value(value: str | float | bool | Decimal | date | datetime | timedelta | UUID | None) -> str:  # noqa: FBT001, PLR0911
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
    """Compile a simple Q object to PL/pgSQL using a row reference (OLD/NEW).

    Only supports single-table field references (no FK traversal).
    For cross-table Q compilation, use :func:`_check_q_to_sql`.
    """
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


# ------------------------------------------------------------------
# Cross-table field resolution (used by CheckConstraintTrigger)
# ------------------------------------------------------------------


def _resolve_field_ref(
    chain: str,
    model: type[Model],
    qn: Callable[[str], str],
) -> tuple[str, str]:
    """Resolve a potentially cross-table field reference to SQL.

    *chain* is a ``__``-separated string like ``"product__stock__gte"``
    where each segment is either a relation hop, a concrete field, or a
    trailing lookup suffix.

    Returns ``(sql_expression, lookup_type)``.  The SQL expression uses
    ``NEW.`` for local columns and nested sub-selects for FK hops.
    """
    parts = chain.split("__")
    current_model = model
    fk_ref: str | None = None  # accumulated PK reference through FK hops

    for i, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            # Everything from here is the lookup suffix.
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
            # Concrete field found — remaining parts (if any) are the lookup.
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
    """Resolve an ``F()`` expression to SQL (no lookup suffix expected)."""
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


# ======================================================================
# UniqueConstraintTrigger
# ======================================================================


class UniqueConstraintTrigger(BaseConstraint):
    """Enforce uniqueness of a field's value across two tables.

    Uses a deferrable constraint trigger that checks the other table
    on INSERT or UPDATE and raises a unique-violation error (SQLSTATE 23505)
    if a duplicate is found.

    Each table in the pair needs its own ``UniqueConstraintTrigger``
    pointing at the other table.  Within-table uniqueness is **not** enforced
    by this constraint — use Django's ``UniqueConstraint`` for that.
    """

    violation_error_code = "cross_table_unique"
    violation_error_message = "This value already exists in a related table."

    def __init__(  # noqa: PLR0913
        self,
        *,
        field: str,
        across: str,
        across_field: str | None = None,
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        self.field = field
        self.across = across
        self.across_field = across_field or field
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _get_across_model(self, apps: Apps) -> type[Model]:
        app_label, model_name = self.across.split(".")
        return apps.get_model(app_label, model_name)

    def _function_name(self, table: str) -> str:
        return _make_fn_name(table, self.name)

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        column = model._meta.get_field(self.field).column  # type: ignore[union-attr]  # noqa: SLF001

        across_model = self._get_across_model(model._meta.apps)  # noqa: SLF001
        across_table = across_model._meta.db_table  # noqa: SLF001
        across_column = across_model._meta.get_field(self.across_field).column  # type: ignore[union-attr]  # noqa: SLF001

        fn = self._function_name(table)
        schema_editor.execute(
            f"CREATE OR REPLACE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NEW.{qn(column)} IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM {qn(across_table)} "
            f"WHERE {qn(across_column)} = NEW.{qn(column)} "
            f"FOR UPDATE"
            f") THEN "
            f"RAISE EXCEPTION "
            f"'Cross-table unique constraint \"%%s\" is violated.', '{self.name}' "
            f"USING ERRCODE = '23505', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        return (
            f"CREATE CONSTRAINT TRIGGER {qn(self.name)} "
            f"AFTER INSERT OR UPDATE OF {qn(column)} ON {qn(table)} "
            f"DEFERRABLE INITIALLY DEFERRED "
            f"FOR EACH ROW EXECUTE FUNCTION {qn(fn)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(self.name)} ON {qn(table)}")
        return f"DROP FUNCTION IF EXISTS {qn(fn)}()"

    # -- Python validation ------------------------------------------

    def validate(
        self,
        model: type[Model],  # noqa: ARG002
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if exclude and self.field in exclude:
            return
        value = getattr(instance, self.field)
        if value is None:
            return
        across_model = self._get_across_model(instance._meta.apps)  # noqa: SLF001
        if across_model.objects.using(using).filter(**{self.across_field: value}).exists():  # type: ignore[attr-defined]
            raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["field"] = self.field
        kwargs["across"] = self.across
        if self.across_field != self.field:
            kwargs["across_field"] = self.across_field
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, UniqueConstraintTrigger):
            return (
                self.name == other.name
                and self.field == other.field
                and self.across == other.across
                and self.across_field == other.across_field
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name, self.field, self.across, self.across_field))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: field={self.field!r} across={self.across!r}>"


# ======================================================================
# CheckConstraintTrigger
# ======================================================================


class CheckConstraintTrigger(BaseConstraint):
    """A check constraint that can reference related tables via FK traversal.

    Works like Django's ``CheckConstraint`` but the ``check`` Q object may
    contain ``F()`` expressions that traverse foreign-key relationships
    (e.g. ``F("product__stock")``).  Enforced by a BEFORE INSERT OR UPDATE
    trigger.
    """

    violation_error_code = "check_constraint_trigger"
    violation_error_message = "Check constraint is violated."

    def __init__(
        self,
        *,
        check: Q,
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        self.check: Q = check  # type: ignore[assignment]
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _function_name(self, table: str) -> str:
        return _make_fn_name(table, self.name)

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)

        check_sql = _check_q_to_sql(self.check, model, qn)

        schema_editor.execute(
            f"CREATE OR REPLACE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NOT ({check_sql}) THEN "
            f"RAISE EXCEPTION "
            f"'Check constraint \"%%s\" is violated.', '{self.name}' "
            f"USING ERRCODE = '23514', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        return (
            f"CREATE TRIGGER {qn(self.name)} "
            f"BEFORE INSERT OR UPDATE ON {qn(table)} "
            f"FOR EACH ROW EXECUTE FUNCTION {qn(fn)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(self.name)} ON {qn(table)}")
        return f"DROP FUNCTION IF EXISTS {qn(fn)}()"

    # -- Python validation ------------------------------------------

    def validate(
        self,
        model: type[Model],  # noqa: ARG002
        instance: Model,  # noqa: ARG002
        exclude: set[str] | None = None,  # noqa: ARG002
        using: str = DEFAULT_DB_ALIAS,  # noqa: ARG002
    ) -> None:
        # Cross-table checks require DB access to resolve FK chains;
        # the trigger is the primary enforcement mechanism.
        return

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["check"] = self.check
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CheckConstraintTrigger):
            return self.name == other.name and self.check == other.check
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name,))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: check={self.check!r}>"


# ======================================================================
# AllowedTransitions
# ======================================================================


class AllowedTransitions(BaseConstraint):
    """Restrict a field to an explicit set of state transitions.

    Uses a BEFORE UPDATE trigger that compares ``OLD.<field>`` to
    ``NEW.<field>`` and rejects any change not listed in *transitions*.
    Inserts are not constrained.
    """

    violation_error_code = "invalid_transition"
    violation_error_message = "This state transition is not allowed."

    def __init__(
        self,
        *,
        field: str,
        transitions: dict[str, list[str]],
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        self.field = field
        self.transitions = transitions
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _function_name(self, table: str) -> str:
        return _make_fn_name(table, self.name)

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        column = model._meta.get_field(self.field).column  # type: ignore[union-attr]  # noqa: SLF001
        fn = self._function_name(table)

        # Build OR-chain using IS NOT DISTINCT FROM for NULL-safe comparison:
        # (OLD.col IS NOT DISTINCT FROM 'a' AND NEW.col IN ('b','c')) OR ...
        conditions: list[str] = []
        for from_state, to_states in self.transitions.items():
            to_vals = ", ".join(_sql_value(s) for s in to_states)
            conditions.append(
                f"(OLD.{qn(column)} IS NOT DISTINCT FROM {_sql_value(from_state)} AND NEW.{qn(column)} IN ({to_vals}))",
            )
        allowed = " OR ".join(conditions) if conditions else "FALSE"

        schema_editor.execute(
            f"CREATE OR REPLACE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NOT ({allowed}) THEN "
            f"RAISE EXCEPTION "
            f"'Transition constraint \"%%s\" is violated: %%s → %%s is not allowed.', "
            f"'{self.name}', OLD.{qn(column)}::text, NEW.{qn(column)}::text "
            f"USING ERRCODE = '23514', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        return (
            f"CREATE TRIGGER {qn(self.name)} "
            f"BEFORE UPDATE OF {qn(column)} ON {qn(table)} "
            f"FOR EACH ROW "
            f"WHEN (OLD.{qn(column)} IS DISTINCT FROM NEW.{qn(column)}) "
            f"EXECUTE FUNCTION {qn(fn)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(self.name)} ON {qn(table)}")
        return f"DROP FUNCTION IF EXISTS {qn(fn)}()"

    # -- Python validation ------------------------------------------

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if exclude and self.field in exclude:
            return
        if instance.pk is None:
            return  # new instance — trigger only constrains UPDATEs

        new_value = getattr(instance, self.field)
        try:
            old_value = (
                model._default_manager.using(using)  # noqa: SLF001
                .values_list(self.field, flat=True)
                .get(pk=instance.pk)
            )
        except model.DoesNotExist:  # type: ignore[attr-defined]
            return

        if old_value == new_value:
            return

        # Look up allowed transitions using the actual old value, not str().
        allowed = self.transitions.get(old_value, [])
        if new_value not in allowed:
            raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["field"] = self.field
        kwargs["transitions"] = self.transitions
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AllowedTransitions):
            return self.name == other.name and self.field == other.field and self.transitions == other.transitions
        return super().__eq__(other)

    def __hash__(self) -> int:
        hashable = tuple(sorted((k, tuple(v)) for k, v in self.transitions.items()))
        return hash((self.name, self.field, hashable))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: field={self.field!r}>"


# ======================================================================
# Immutable
# ======================================================================


class Immutable(BaseConstraint):
    """Prevent changes to specific fields, optionally conditioned on row state.

    Uses a BEFORE UPDATE trigger.  When *when* is provided (a ``Q`` object),
    the fields are only immutable while the **OLD** row matches the condition.
    """

    violation_error_code = "immutable_field"
    violation_error_message = "This field cannot be changed."

    def __init__(
        self,
        *,
        fields: list[str],
        when: Q | None = None,
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        if not fields:
            msg = "Immutable constraint requires at least one field."
            raise ValueError(msg)
        self.fields = list(fields)
        self.when = when
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _function_name(self, table: str) -> str:
        return _make_fn_name(table, self.name)

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return ""

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)

        # Build "any field changed?" check
        changed_parts = []
        for field_name in self.fields:
            col = qn(model._meta.get_field(field_name).column)  # type: ignore[union-attr]  # noqa: SLF001
            changed_parts.append(f"OLD.{col} IS DISTINCT FROM NEW.{col}")
        changed_check = " OR ".join(changed_parts)

        # Optional condition on OLD row
        if self.when is not None:
            when_sql = _q_to_sql(self.when, model, qn, row_ref="OLD")
            full_check = f"({when_sql}) AND ({changed_check})"
        else:
            full_check = changed_check

        schema_editor.execute(
            f"CREATE OR REPLACE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF {full_check} THEN "
            f"RAISE EXCEPTION "
            f"'Immutability constraint \"%%s\" is violated.', '{self.name}' "
            f"USING ERRCODE = '23514', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        return f"CREATE TRIGGER {qn(self.name)} BEFORE UPDATE ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn)}()"

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name(table)
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(self.name)} ON {qn(table)}")
        return f"DROP FUNCTION IF EXISTS {qn(fn)}()"

    # -- Python validation ------------------------------------------

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if instance.pk is None:
            return

        check_fields = [f for f in self.fields if f not in (exclude or set())]
        if not check_fields:
            return

        # Fetch old row
        qs = model._default_manager.using(using).filter(pk=instance.pk)  # noqa: SLF001
        if self.when is not None:
            qs = qs.filter(self.when)
        try:
            old_values = qs.values(*check_fields).get()
        except model.DoesNotExist:  # type: ignore[attr-defined]
            return  # row doesn't exist or condition not met

        for field_name in check_fields:
            if old_values[field_name] != getattr(instance, field_name):
                raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["fields"] = self.fields
        if self.when is not None:
            kwargs["when"] = self.when
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Immutable):
            return self.name == other.name and self.fields == other.fields and self.when == other.when
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name, tuple(self.fields), self.when))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: fields={self.fields!r}>"


# ======================================================================
# MaintainedCount
# ======================================================================


class MaintainedCount(BaseConstraint):
    """Keep a denormalised count field in sync via INSERT/UPDATE/DELETE triggers.

    Declared on the **child** model (the one with the FK).  Creates triggers
    on the child table that atomically adjust a counter on the target
    (parent) table.
    """

    violation_error_code = None
    violation_error_message = ""

    def __init__(  # noqa: PLR0913
        self,
        *,
        target: str,
        target_field: str,
        fk_field: str,
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        self.target = target
        self.target_field = target_field
        self.fk_field = fk_field
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _get_target_model(self, apps: Apps) -> type[Model]:
        app_label, model_name = self.target.split(".")
        return apps.get_model(app_label, model_name)

    # -- Schema SQL -------------------------------------------------

    def _build_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> list[str]:
        """Return all DDL statements (functions + triggers) as a list."""
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fk_col = qn(model._meta.get_field(self.fk_field).column)  # type: ignore[union-attr]  # noqa: SLF001

        target_model = self._get_target_model(model._meta.apps)  # noqa: SLF001
        t_table = qn(target_model._meta.db_table)  # noqa: SLF001
        t_pk = qn(target_model._meta.pk.column)  # noqa: SLF001
        t_cnt = qn(target_model._meta.get_field(self.target_field).column)  # type: ignore[union-attr]  # noqa: SLF001

        n = self.name
        stmts: list[str] = []

        # --- INSERT ---
        fn_ins = _make_fn_name(table, n, "_ins")
        stmts.append(
            f"CREATE OR REPLACE FUNCTION {qn(fn_ins)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NEW.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1 WHERE {t_pk} = NEW.{fk_col}; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_ins')} AFTER INSERT ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn_ins)}()",
        )

        # --- DELETE ---
        fn_del = _make_fn_name(table, n, "_del")
        stmts.append(
            f"CREATE OR REPLACE FUNCTION {qn(fn_del)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF OLD.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1 WHERE {t_pk} = OLD.{fk_col}; "
            f"END IF; "
            f"RETURN OLD; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_del')} AFTER DELETE ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn_del)}()",
        )

        # --- UPDATE (FK reassignment) ---
        fn_upd = _make_fn_name(table, n, "_upd")
        stmts.append(
            f"CREATE OR REPLACE FUNCTION {qn(fn_upd)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF OLD.{fk_col} IS DISTINCT FROM NEW.{fk_col} THEN "
            f"IF OLD.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1 WHERE {t_pk} = OLD.{fk_col}; "
            f"END IF; "
            f"IF NEW.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1 WHERE {t_pk} = NEW.{fk_col}; "
            f"END IF; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql",
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_upd')} "
            f"AFTER UPDATE OF {fk_col} ON {qn(table)} "
            f"FOR EACH ROW EXECUTE FUNCTION {qn(fn_upd)}()",
        )
        return stmts

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        # Defer ALL statements (functions + triggers) until after CREATE TABLE.
        schema_editor.deferred_sql.extend(self._build_sql(model, schema_editor))
        return ""

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        # Called by add_constraint — table already exists, execute immediately.
        stmts = self._build_sql(model, schema_editor)
        for sql in stmts[:-1]:
            schema_editor.execute(sql)
        return stmts[-1]

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:  # type: ignore[override]
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        n = self.name

        for suffix in ("_ins", "_del", "_upd"):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(n + suffix)} ON {qn(table)}")
            fn = _make_fn_name(table, n, suffix)
            schema_editor.execute(f"DROP FUNCTION IF EXISTS {qn(fn)}()")
        return ""  # everything already dropped above

    # -- Python validation ------------------------------------------

    def validate(
        self,
        model: type[Model],  # noqa: ARG002
        instance: Model,  # noqa: ARG002
        exclude: set[str] | None = None,  # noqa: ARG002
        using: str = DEFAULT_DB_ALIAS,  # noqa: ARG002
    ) -> None:
        # MaintainedCount is a data-sync mechanism, not a user-input constraint.
        return

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, Sequence[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["target"] = self.target
        kwargs["target_field"] = self.target_field
        kwargs["fk_field"] = self.fk_field
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MaintainedCount):
            return (
                self.name == other.name
                and self.target == other.target
                and self.target_field == other.target_field
                and self.fk_field == other.fk_field
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name, self.target, self.target_field, self.fk_field))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: target={self.target!r} field={self.target_field!r}>"
