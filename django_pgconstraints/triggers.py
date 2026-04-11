"""pgtrigger-based trigger classes for django-pgconstraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pgtrigger
import pgtrigger.utils

from django_pgconstraints.sql import _check_q_to_sql, _q_to_sql, _sql_value

if TYPE_CHECKING:
    from django.db.models import Model, Q

# ======================================================================
# UniqueConstraintTrigger
# ======================================================================


class UniqueConstraintTrigger(pgtrigger.Trigger):
    """Deferrable cross-table uniqueness enforced via advisory lock + existence check."""

    when = pgtrigger.After
    operation = pgtrigger.Insert | pgtrigger.Update
    timing = pgtrigger.Deferred

    def __init__(
        self,
        *,
        field: str,
        across: str,
        across_field: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.field = field
        self.across = across
        self.across_field = across_field or field
        super().__init__(**kwargs)

    def get_func(self, model: type[Model]) -> str:
        from django.apps import apps  # noqa: PLC0415

        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)  # noqa: SLF001

        app_label, model_name = self.across.split(".")
        across_model = apps.get_model(app_label, model_name)
        across_table = qn(across_model._meta.db_table)  # noqa: SLF001
        across_column = qn(across_model._meta.get_field(self.across_field).column)  # noqa: SLF001

        return self.format_sql(f"""
            IF NEW.{column} IS NOT NULL THEN
                PERFORM pg_advisory_xact_lock(hashtext(NEW.{column}::text));
                IF EXISTS (
                    SELECT 1 FROM {across_table}
                    WHERE {across_column} = NEW.{column}
                    FOR UPDATE
                ) THEN
                    RAISE EXCEPTION
                        'Cross-table unique constraint "%s" is violated.', '{self.name}'
                        USING ERRCODE = '23505', CONSTRAINT = '{self.name}';
                END IF;
            END IF;
            RETURN NEW;
        """)


# ======================================================================
# CheckConstraintTrigger
# ======================================================================


class CheckConstraintTrigger(pgtrigger.Trigger):
    """Check constraint supporting FK traversal via Q objects."""

    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    def __init__(self, *, check: Q, **kwargs: Any) -> None:  # noqa: ANN401
        self.check = check
        super().__init__(**kwargs)

    def get_func(self, model: type[Model]) -> str:
        qn = pgtrigger.utils.quote
        check_sql = _check_q_to_sql(self.check, model, qn)

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
    """Restrict a field to an explicit set of state transitions."""

    when = pgtrigger.Before
    operation = pgtrigger.Update

    def __init__(self, *, field: str, transitions: dict, **kwargs: Any) -> None:  # noqa: ANN401
        self.field = field
        self.transitions = transitions
        super().__init__(**kwargs)

    def get_condition(self, model: type[Model]) -> pgtrigger.Condition:
        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)  # noqa: SLF001
        return pgtrigger.Condition(f"OLD.{column} IS DISTINCT FROM NEW.{column}")

    def get_func(self, model: type[Model]) -> str:
        qn = pgtrigger.utils.quote
        column = qn(model._meta.get_field(self.field).column)  # noqa: SLF001

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
                    'Transition constraint "%s" is violated: %s → %s is not allowed.',
                    '{self.name}', OLD.{column}::text, NEW.{column}::text
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


# ======================================================================
# Immutable
# ======================================================================


class Immutable(pgtrigger.Trigger):
    """Prevent changes to specific fields, optionally conditioned on OLD row state."""

    when = pgtrigger.Before
    operation = pgtrigger.Update

    def __init__(
        self,
        *,
        fields: list[str],
        when_condition: Q | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if not fields:
            msg = "Immutable constraint requires at least one field."
            raise ValueError(msg)
        self.fields = list(fields)
        self.when_condition = when_condition
        super().__init__(**kwargs)

    def get_func(self, model: type[Model]) -> str:
        qn = pgtrigger.utils.quote

        changed_parts = []
        for field_name in self.fields:
            col = qn(model._meta.get_field(field_name).column)  # noqa: SLF001
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
                    'Immutability constraint "%s" is violated.', '{self.name}'
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)


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

    def _resolve_target(self, model: type[Model]) -> tuple[str, str, str, str]:
        from django.apps import apps  # noqa: PLC0415

        qn = pgtrigger.utils.quote
        fk_col = qn(model._meta.get_field(self.fk_field).column)  # noqa: SLF001

        app_label, model_name = self.target.split(".")
        target_model = apps.get_model(app_label, model_name)
        t_table = qn(target_model._meta.db_table)  # noqa: SLF001
        t_pk = qn(target_model._meta.pk.column)  # noqa: SLF001
        t_cnt = qn(target_model._meta.get_field(self.target_field).column)  # noqa: SLF001

        return fk_col, t_table, t_pk, t_cnt


class _MaintainedCountInsert(_MaintainedCountBase):
    when = pgtrigger.After
    operation = pgtrigger.Insert

    def get_func(self, model: type[Model]) -> str:
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

    def get_func(self, model: type[Model]) -> str:
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

    def get_func(self, model: type[Model]) -> str:
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
        common = {"target": target, "target_field": target_field, "fk_field": fk_field}
        return [
            _MaintainedCountInsert(name=f"{name}_ins", **common),
            _MaintainedCountDelete(name=f"{name}_del", **common),
            _MaintainedCountUpdate(name=f"{name}_upd", **common),
        ]
