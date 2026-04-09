"""Constraint implementations backed by PostgreSQL triggers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db.models import BaseConstraint, Q

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.apps import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.models import Model


# ------------------------------------------------------------------
# SQL helpers
# ------------------------------------------------------------------


def _sql_value(value: str | float | bool | None) -> str:  # noqa: FBT001
    """Convert a Python value to a SQL literal for use in PL/pgSQL."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    msg = f"Cannot convert {type(value).__name__} to SQL literal"
    raise TypeError(msg)


def _q_to_sql(q: Q, model: type[Model], qn: Callable[[str], str], row_ref: str = "OLD") -> str:
    """Compile a Q object to PL/pgSQL using a row reference (OLD/NEW)."""
    parts: list[str] = []
    for child in q.children:
        if isinstance(child, Q):
            parts.append(f"({_q_to_sql(child, model, qn, row_ref)})")
        else:
            lookup_str, value = child
            if "__" in lookup_str:
                field_name, lookup_type = lookup_str.rsplit("__", 1)
            else:
                field_name, lookup_type = lookup_str, "exact"

            column = model._meta.get_field(field_name).column  # noqa: SLF001
            col = f"{row_ref}.{qn(column)}"

            if lookup_type == "exact":
                parts.append(f"{col} IS NULL" if value is None else f"{col} = {_sql_value(value)}")
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


# ======================================================================
# CrossTableUnique
# ======================================================================


class CrossTableUnique(BaseConstraint):
    """Enforce uniqueness of a field's value across two tables.

    Uses a deferrable constraint trigger that checks the other table
    on INSERT or UPDATE and raises a unique-violation error (SQLSTATE 23505)
    if a duplicate is found.

    Each table in the pair needs its own ``CrossTableUnique`` constraint
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

    def _function_name(self) -> str:
        return f"pgc_fn_{self.name}"

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> None:
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        column = model._meta.get_field(self.field).column  # noqa: SLF001

        across_model = self._get_across_model(model._meta.apps)  # noqa: SLF001
        across_table = across_model._meta.db_table  # noqa: SLF001
        across_column = across_model._meta.get_field(self.across_field).column  # noqa: SLF001

        fn = self._function_name()
        schema_editor.execute(
            f"CREATE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
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
            f"END; $body$ LANGUAGE plpgsql"
        )
        return (
            f"CREATE CONSTRAINT TRIGGER {qn(self.name)} "
            f"AFTER INSERT OR UPDATE OF {qn(column)} ON {qn(table)} "
            f"DEFERRABLE INITIALLY DEFERRED "
            f"FOR EACH ROW EXECUTE FUNCTION {qn(fn)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name()
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
        if across_model.objects.using(using).filter(**{self.across_field: value}).exists():
            raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["field"] = self.field
        kwargs["across"] = self.across
        if self.across_field != self.field:
            kwargs["across_field"] = self.across_field
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CrossTableUnique):
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

    def _function_name(self) -> str:
        return f"pgc_fn_{self.name}"

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> None:
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        column = model._meta.get_field(self.field).column  # noqa: SLF001
        fn = self._function_name()

        # Build OR-chain: (OLD.col = 'a' AND NEW.col IN ('b','c')) OR ...
        conditions: list[str] = []
        for from_state, to_states in self.transitions.items():
            to_vals = ", ".join(_sql_value(s) for s in to_states)
            conditions.append(f"(OLD.{qn(column)} = {_sql_value(from_state)} AND NEW.{qn(column)} IN ({to_vals}))")
        allowed = " OR ".join(conditions) if conditions else "FALSE"

        schema_editor.execute(
            f"CREATE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NOT ({allowed}) THEN "
            f"RAISE EXCEPTION "
            f"'Transition constraint \"%%s\" is violated: %%s → %%s is not allowed.', "
            f"'{self.name}', OLD.{qn(column)}::text, NEW.{qn(column)}::text "
            f"USING ERRCODE = '23514', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql"
        )
        return (
            f"CREATE TRIGGER {qn(self.name)} "
            f"BEFORE UPDATE OF {qn(column)} ON {qn(table)} "
            f"FOR EACH ROW "
            f"WHEN (OLD.{qn(column)} IS DISTINCT FROM NEW.{qn(column)}) "
            f"EXECUTE FUNCTION {qn(fn)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name()
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
        except model.DoesNotExist:
            return

        if old_value == new_value:
            return

        allowed = self.transitions.get(str(old_value), [])
        if str(new_value) not in allowed:
            raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
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
        return hash((self.name, self.field, tuple(sorted(self.transitions.items()))))

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
        self.fields = list(fields)
        self.when = when
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    def _function_name(self) -> str:
        return f"pgc_fn_{self.name}"

    # -- Schema SQL -------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> None:
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name()

        # Build "any field changed?" check
        changed_parts = []
        for field_name in self.fields:
            col = qn(model._meta.get_field(field_name).column)  # noqa: SLF001
            changed_parts.append(f"OLD.{col} IS DISTINCT FROM NEW.{col}")
        changed_check = " OR ".join(changed_parts)

        # Optional condition on OLD row
        if self.when is not None:
            when_sql = _q_to_sql(self.when, model, qn, row_ref="OLD")
            full_check = f"({when_sql}) AND ({changed_check})"
        else:
            full_check = changed_check

        schema_editor.execute(
            f"CREATE FUNCTION {qn(fn)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF {full_check} THEN "
            f"RAISE EXCEPTION "
            f"'Immutability constraint \"%%s\" is violated.', '{self.name}' "
            f"USING ERRCODE = '23514', CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql"
        )
        return f"CREATE TRIGGER {qn(self.name)} BEFORE UPDATE ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn)}()"

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn = self._function_name()
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
        except model.DoesNotExist:
            return  # row doesn't exist or condition not met

        for field_name in check_fields:
            if old_values[field_name] != getattr(instance, field_name):
                raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # -- Serialisation ----------------------------------------------

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
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
        return hash((self.name, tuple(self.fields)))

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
        fk_col = qn(model._meta.get_field(self.fk_field).column)  # noqa: SLF001

        target_model = self._get_target_model(model._meta.apps)  # noqa: SLF001
        t_table = qn(target_model._meta.db_table)  # noqa: SLF001
        t_pk = qn(target_model._meta.pk.column)  # noqa: SLF001
        t_cnt = qn(target_model._meta.get_field(self.target_field).column)  # noqa: SLF001

        n = self.name
        stmts: list[str] = []

        # --- INSERT ---
        fn_ins = f"pgc_fn_{n}_ins"
        stmts.append(
            f"CREATE FUNCTION {qn(fn_ins)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NEW.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1 WHERE {t_pk} = NEW.{fk_col}; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; $body$ LANGUAGE plpgsql"
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_ins')} AFTER INSERT ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn_ins)}()"
        )

        # --- DELETE ---
        fn_del = f"pgc_fn_{n}_del"
        stmts.append(
            f"CREATE FUNCTION {qn(fn_del)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF OLD.{fk_col} IS NOT NULL THEN "
            f"UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1 WHERE {t_pk} = OLD.{fk_col}; "
            f"END IF; "
            f"RETURN OLD; "
            f"END; $body$ LANGUAGE plpgsql"
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_del')} AFTER DELETE ON {qn(table)} FOR EACH ROW EXECUTE FUNCTION {qn(fn_del)}()"
        )

        # --- UPDATE (FK reassignment) ---
        fn_upd = f"pgc_fn_{n}_upd"
        stmts.append(
            f"CREATE FUNCTION {qn(fn_upd)}() RETURNS TRIGGER AS $body$ "
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
            f"END; $body$ LANGUAGE plpgsql"
        )
        stmts.append(
            f"CREATE TRIGGER {qn(n + '_upd')} "
            f"AFTER UPDATE OF {fk_col} ON {qn(table)} "
            f"FOR EACH ROW EXECUTE FUNCTION {qn(fn_upd)}()"
        )
        return stmts

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> None:
        # Defer ALL statements (functions + triggers) until after CREATE TABLE.
        schema_editor.deferred_sql.extend(self._build_sql(model, schema_editor))

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        # Called by add_constraint — table already exists, execute immediately.
        stmts = self._build_sql(model, schema_editor)
        for sql in stmts[:-1]:
            schema_editor.execute(sql)
        return stmts[-1]

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        n = self.name

        for suffix in ("ins", "del", "upd"):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(n + '_' + suffix)} ON {qn(table)}")
            schema_editor.execute(f"DROP FUNCTION IF EXISTS {qn('pgc_fn_' + n + '_' + suffix)}()")
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

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, Any]]:
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
