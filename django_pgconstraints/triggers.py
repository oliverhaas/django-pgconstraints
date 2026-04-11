"""pgtrigger-based trigger classes for django-pgconstraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pgtrigger
import pgtrigger.utils
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection
from django.db.models import Deferrable
from django.db.models.sql import Query

from django_pgconstraints.sql import _check_q_to_sql, _q_to_sql, _resolve_field_ref, _sql_value

if TYPE_CHECKING:
    from django.db.models import Model, Q
    from django.db.models.expressions import BaseExpression


# ======================================================================
# UniqueConstraintTrigger
# ======================================================================


def _compile_expression(expr: BaseExpression, model: type[Model], row_ref: str = "NEW") -> str:
    """Compile a Django expression to SQL, prefixing column refs with *row_ref*.

    Django's ``resolve_expression`` with ``alias_cols=False`` produces bare
    ``"column_name"`` references.  We replace each with ``{row_ref}."column_name"``.
    """
    query = Query(model=model, alias_cols=False)
    resolved = expr.resolve_expression(query, allow_joins=False, for_save=True)
    compiler = query.get_compiler(connection=connection)
    sql, params = resolved.as_sql(compiler, connection)
    if params:
        sql = sql % tuple(compiler.connection.schema_editor().quote_value(p) for p in params)

    # Prefix bare column references with the row reference.
    for field in model._meta.fields:  # noqa: SLF001
        quoted_col = f'"{field.column}"'
        sql = sql.replace(quoted_col, f"{row_ref}.{quoted_col}")

    return sql


class UniqueConstraintTrigger(pgtrigger.Trigger):
    """Enforce uniqueness of field values, with FK-traversal and expression support.

    Drop-in trigger replacement for Django's ``UniqueConstraint``.

    *fields* can contain:
    - plain field names (``"slug"``)
    - ``__``-separated FK chains (``"book__author"``)

    *expressions* can contain Django expressions (``Lower("email")``).

    At least one of *fields* or *expressions* must be provided.

    Set ``deferrable=Deferrable.DEFERRED`` for a constraint trigger that
    fires at commit time (default ``None`` — fires immediately).
    """

    when = pgtrigger.After
    operation = pgtrigger.Insert | pgtrigger.Update

    violation_error_code: str = "unique"
    violation_error_message: str = "This value already exists."

    def __init__(  # noqa: PLR0913
        self,
        *expressions: BaseExpression,
        fields: list[str] | tuple[str, ...] = (),
        condition: Q | None = None,
        deferrable: Deferrable | None = None,
        nulls_distinct: bool | None = None,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if not fields and not expressions:
            msg = "At least one field or expression is required."
            raise ValueError(msg)
        if fields and expressions:
            msg = "UniqueConstraintTrigger.fields and expressions are mutually exclusive."
            raise ValueError(msg)
        if expressions and deferrable:
            msg = "UniqueConstraintTrigger with expressions cannot be deferred."
            raise ValueError(msg)

        self.fields = list(fields)
        self.expressions = list(expressions)
        self.unique_condition = condition
        self.nulls_distinct = nulls_distinct

        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message

        if deferrable == Deferrable.DEFERRED:
            kwargs.setdefault("timing", pgtrigger.Deferred)
        elif deferrable == Deferrable.IMMEDIATE:
            kwargs.setdefault("timing", pgtrigger.Immediate)

        super().__init__(**kwargs)

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote
        table = qn(model._meta.db_table)  # noqa: SLF001
        pk_col = qn(model._meta.pk.column)  # noqa: SLF001

        # Resolve each field/expression for both NEW row and existing rows.
        new_exprs: list[str] = []
        existing_exprs: list[str] = []

        for field_chain in self.fields:
            new_sql, _ = _resolve_field_ref(field_chain, model, qn, row_ref="NEW")  # type: ignore[arg-type]
            exist_sql, _ = _resolve_field_ref(field_chain, model, qn, row_ref="existing")  # type: ignore[arg-type]
            new_exprs.append(new_sql)
            existing_exprs.append(exist_sql)

        for expr in self.expressions:
            new_exprs.append(_compile_expression(expr, model, row_ref="NEW"))  # type: ignore[arg-type]
            existing_exprs.append(_compile_expression(expr, model, row_ref="existing"))  # type: ignore[arg-type]

        # NULL handling
        if self.nulls_distinct is False:
            null_guard = ""
            comparisons = [f"{ex} IS NOT DISTINCT FROM {nw}" for ex, nw in zip(existing_exprs, new_exprs, strict=True)]
        else:
            null_checks = " OR ".join(f"({nw}) IS NULL" for nw in new_exprs)
            null_guard = f"IF NOT ({null_checks}) THEN"
            comparisons = [f"{ex} = {nw}" for ex, nw in zip(existing_exprs, new_exprs, strict=True)]

        where_clause = " AND ".join([*comparisons, f"existing.{pk_col} IS DISTINCT FROM NEW.{pk_col}"])

        # Advisory lock
        if len(new_exprs) == 1:
            lock_expr = f"hashtext(({new_exprs[0]})::text)"
        else:
            concat_parts = " || ',' || ".join(f"COALESCE(({nw})::text, '')" for nw in new_exprs)
            lock_expr = f"hashtext({concat_parts})"

        # Condition guard (partial unique)
        condition_sql = ""
        if self.unique_condition is not None:
            condition_sql = _q_to_sql(self.unique_condition, model, qn, row_ref="NEW")  # type: ignore[arg-type]

        cond_open = f"IF {condition_sql} THEN " if condition_sql else ""
        cond_close = "END IF; " if condition_sql else ""
        null_open = f"{null_guard} " if null_guard else ""
        null_close = "END IF; " if null_guard else ""

        return self.format_sql(f"""
            {cond_open}
            {null_open}
                PERFORM pg_advisory_xact_lock({lock_expr});
                IF EXISTS (
                    SELECT 1 FROM {table} existing
                    WHERE {where_clause}
                    FOR UPDATE
                ) THEN
                    RAISE EXCEPTION
                        'Unique constraint "%s" is violated.', '{self.name}'
                        USING ERRCODE = '23505', CONSTRAINT = '{self.name}';
                END IF;
            {null_close}
            {cond_close}
            RETURN NEW;
        """)

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        """Python-level validation, compatible with Django's full_clean()."""
        if exclude and any(f.split("__")[0] in exclude for f in self.fields):
            return

        # Resolve field values — for FK chains, traverse the related objects.
        values: dict[str, Any] = {}
        for field_chain in self.fields:
            parts = field_chain.split("__")
            obj: Any = instance
            for part in parts:
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            values[field_chain] = obj

        # Default (nulls_distinct is not False): NULLs never violate uniqueness.
        if self.nulls_distinct is not False and any(v is None for v in values.values()):
            return

        # Build queryset with ORM-style dunder lookups.
        lookup = dict(values)
        qs = model._default_manager.using(using).filter(**lookup)  # noqa: SLF001
        if not instance._state.adding and instance.pk is not None:  # noqa: SLF001
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise ValidationError(
                self.violation_error_message,
                code=self.violation_error_code,
            )


# ======================================================================
# CheckConstraintTrigger
# ======================================================================


class CheckConstraintTrigger(pgtrigger.Trigger):
    """Check constraint supporting FK traversal via Q objects.

    Raises SQLSTATE 23514 on violation.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    violation_error_code: str = "check_constraint_trigger"
    violation_error_message: str = "Check constraint is violated."

    def __init__(
        self,
        *,
        check: Q,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.check = check
        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message
        super().__init__(**kwargs)

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote
        check_sql = _check_q_to_sql(self.check, model, qn)  # type: ignore[arg-type]

        return self.format_sql(f"""
            IF NOT ({check_sql}) THEN
                RAISE EXCEPTION
                    'Check constraint "%s" is violated.', '{self.name}'
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


# ======================================================================
# AllowedTransitions
# ======================================================================


class AllowedTransitions(pgtrigger.Trigger):
    """Restrict a field to an explicit set of state transitions.

    Raises SQLSTATE 23514 on violation.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Update

    violation_error_code: str = "invalid_transition"
    violation_error_message: str = "This state transition is not allowed."

    def __init__(
        self,
        *,
        field: str,
        transitions: dict[str, list[str]],
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.field = field
        self.transitions = transitions
        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message
        super().__init__(**kwargs)

    def get_condition(self, model: Model) -> pgtrigger.Condition:
        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)  # type: ignore[union-attr]  # noqa: SLF001
        return pgtrigger.Condition(f"OLD.{column} IS DISTINCT FROM NEW.{column}")

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)  # type: ignore[union-attr]  # noqa: SLF001

        conditions: list[str] = []
        for from_state, to_states in self.transitions.items():
            to_vals = ", ".join(_sql_value(s) for s in to_states)
            conditions.append(
                f"(OLD.{column} IS NOT DISTINCT FROM {_sql_value(from_state)} AND NEW.{column} IN ({to_vals}))",
            )
        allowed = " OR ".join(conditions) if conditions else "FALSE"

        return self.format_sql(f"""
            IF NOT ({allowed}) THEN
                RAISE EXCEPTION
                    'Transition constraint "%s" is violated: %s -> %s is not allowed.',
                    '{self.name}', OLD.{column}::text, NEW.{column}::text
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        """Python-level validation, compatible with Django's full_clean()."""
        if exclude and self.field in exclude:
            return
        if instance.pk is None:
            return

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

        allowed = self.transitions.get(old_value, [])
        if new_value not in allowed:
            raise ValidationError(
                self.violation_error_message,
                code=self.violation_error_code,
            )


# ======================================================================
# Immutable
# ======================================================================


class Immutable(pgtrigger.Trigger):
    """Prevent changes to specific fields, optionally conditioned on OLD row state.

    Raises SQLSTATE 23514 on violation.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Update

    violation_error_code: str = "immutable_field"
    violation_error_message: str = "This field cannot be changed."

    def __init__(
        self,
        *,
        fields: list[str],
        when_condition: Q | None = None,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if not fields:
            msg = "Immutable trigger requires at least one field."
            raise ValueError(msg)
        self.fields = list(fields)
        self.when_condition = when_condition
        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message
        super().__init__(**kwargs)

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote

        changed_parts = []
        for field_name in self.fields:
            col = qn(model._meta.get_field(field_name).column)  # type: ignore[union-attr]  # noqa: SLF001
            changed_parts.append(f"OLD.{col} IS DISTINCT FROM NEW.{col}")
        changed_check = " OR ".join(changed_parts)

        if self.when_condition is not None:
            when_sql = _q_to_sql(self.when_condition, model, qn, row_ref="OLD")  # type: ignore[arg-type]
            full_check = f"({when_sql}) AND ({changed_check})"
        else:
            full_check = changed_check

        return self.format_sql(f"""
            IF {full_check} THEN
                RAISE EXCEPTION
                    'Immutability constraint "%s" is violated.', '{self.name}'
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        """Python-level validation, compatible with Django's full_clean()."""
        if instance.pk is None:
            return

        check_fields = [f for f in self.fields if f not in (exclude or set())]
        if not check_fields:
            return

        qs = model._default_manager.using(using).filter(pk=instance.pk)  # noqa: SLF001
        if self.when_condition is not None:
            qs = qs.filter(self.when_condition)
        try:
            old_values = qs.values(*check_fields).get()
        except model.DoesNotExist:  # type: ignore[attr-defined]
            return

        for field_name in check_fields:
            if old_values[field_name] != getattr(instance, field_name):
                raise ValidationError(
                    self.violation_error_message,
                    code=self.violation_error_code,
                )


# ======================================================================
# MaintainedCount
# ======================================================================


class _MaintainedCountBase(pgtrigger.Trigger):
    """Shared base for the three MaintainedCount triggers."""

    def __init__(
        self,
        *,
        target: str,
        target_field: str,
        fk_field: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.target = target
        self.target_field = target_field
        self.fk_field = fk_field
        super().__init__(**kwargs)

    def _resolve_target(self, model: Model) -> tuple[str, str, str, str]:
        from django.apps import apps  # noqa: PLC0415

        qn = pgtrigger.utils.quote
        fk_col = qn(model._meta.get_field(self.fk_field).column)  # type: ignore[union-attr]  # noqa: SLF001

        app_label, model_name = self.target.split(".")
        target_model = apps.get_model(app_label, model_name)
        t_table = qn(target_model._meta.db_table)  # noqa: SLF001
        t_pk = qn(target_model._meta.pk.column)  # noqa: SLF001
        t_cnt = qn(target_model._meta.get_field(self.target_field).column)  # noqa: SLF001

        return fk_col, t_table, t_pk, t_cnt


class _MaintainedCountInsert(_MaintainedCountBase):
    when = pgtrigger.After
    operation = pgtrigger.Insert

    def get_func(self, model: Model) -> str:
        fk_col, t_table, t_pk, t_cnt = self._resolve_target(model)
        return self.format_sql(f"""
            IF NEW.{fk_col} IS NOT NULL THEN
                UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1
                WHERE {t_pk} = NEW.{fk_col};
            END IF;
            RETURN NEW;
        """)


class _MaintainedCountDelete(_MaintainedCountBase):
    when = pgtrigger.After
    operation = pgtrigger.Delete

    def get_func(self, model: Model) -> str:
        fk_col, t_table, t_pk, t_cnt = self._resolve_target(model)
        return self.format_sql(f"""
            IF OLD.{fk_col} IS NOT NULL THEN
                UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1
                WHERE {t_pk} = OLD.{fk_col};
            END IF;
            RETURN OLD;
        """)


class _MaintainedCountUpdate(_MaintainedCountBase):
    when = pgtrigger.After
    operation = pgtrigger.Update

    def get_func(self, model: Model) -> str:
        fk_col, t_table, t_pk, t_cnt = self._resolve_target(model)
        return self.format_sql(f"""
            IF OLD.{fk_col} IS DISTINCT FROM NEW.{fk_col} THEN
                IF OLD.{fk_col} IS NOT NULL THEN
                    UPDATE {t_table} SET {t_cnt} = {t_cnt} - 1
                    WHERE {t_pk} = OLD.{fk_col};
                END IF;
                IF NEW.{fk_col} IS NOT NULL THEN
                    UPDATE {t_table} SET {t_cnt} = {t_cnt} + 1
                    WHERE {t_pk} = NEW.{fk_col};
                END IF;
            END IF;
            RETURN NEW;
        """)


class MaintainedCount:
    """Factory that produces three pgtrigger.Trigger instances for count maintenance."""

    @classmethod
    def triggers(
        cls,
        *,
        name: str,
        target: str,
        target_field: str,
        fk_field: str,
    ) -> list[pgtrigger.Trigger]:
        """Create insert/delete/update triggers for maintaining a count field."""
        common = {"target": target, "target_field": target_field, "fk_field": fk_field}
        return [
            _MaintainedCountInsert(name=f"{name}_ins", **common),
            _MaintainedCountDelete(name=f"{name}_del", **common),
            _MaintainedCountUpdate(name=f"{name}_upd", **common),
        ]
