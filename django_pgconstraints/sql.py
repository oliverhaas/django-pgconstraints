"""SQL helper functions for compiling Q objects to PL/pgSQL."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import FieldDoesNotExist
from django.db.models import F, Q

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model

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
    *,
    row_ref: str = "NEW",
) -> tuple[str, str]:
    """Resolve a potentially cross-table field reference to SQL.

    *chain* is a ``__``-separated string like ``"product__stock__gte"``
    where each segment is either a relation hop, a concrete field, or a
    trailing lookup suffix.

    *row_ref* is the trigger row reference for local columns (``NEW``,
    ``OLD``, or a bare table alias like ``"t"``).

    Returns ``(sql_expression, lookup_type)``.  The SQL expression uses
    *row_ref* for local columns and nested sub-selects for FK hops.
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
                fk_ref = f"{row_ref}.{qn(fk_column)}"
            else:
                tbl = qn(current_model._meta.db_table)  # noqa: SLF001
                pk = qn(current_model._meta.pk.column)  # noqa: SLF001
                fk_ref = f"(SELECT {qn(fk_column)} FROM {tbl} WHERE {pk} = {fk_ref})"
            current_model = related_model  # type: ignore[assignment]
        else:
            # Concrete field found — remaining parts (if any) are the lookup.
            col = qn(field.column)  # type: ignore[union-attr]
            if fk_ref is None:
                sql = f"{row_ref}.{col}"
            else:
                tbl = qn(current_model._meta.db_table)  # noqa: SLF001
                pk = qn(current_model._meta.pk.column)  # noqa: SLF001
                sql = f"(SELECT {col} FROM {tbl} WHERE {pk} = {fk_ref})"
            lookup = "__".join(parts[i + 1 :]) or "exact"
            return sql, lookup

    # Chain ends at a relation — use the FK column value (the related PK).
    if fk_ref is not None:
        return fk_ref, "exact"

    msg = f"Field chain '{chain}' could not be resolved"
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
