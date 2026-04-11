"""SQL helper functions for compiling Q objects to PL/pgSQL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.models import F, Q
from django.db.models.expressions import RawSQL
from django.db.models.sql import Query

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Field, Model

# ------------------------------------------------------------------
# FK-chain field resolution
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


# ------------------------------------------------------------------
# Q compilation via Django's lookup machinery
# ------------------------------------------------------------------


def _resolve_lhs(
    chain: str,
    model: type[Model],
    qn: Callable[[str], str],
    *,
    row_ref: str,
) -> tuple[str, list[str], Field]:
    """Resolve a dunder-path key to ``(sql, remaining_parts, field)``.

    Walks FK hops producing nested sub-selects for cross-table references.
    Stops at the first non-relation field and returns the rest of the chain
    as *remaining_parts* so Django's ``build_lookup`` can process transforms
    and the final lookup operator.

    Used by :func:`_compile_q` to generate the LHS of each Q leaf.  The
    returned *field* is used as the ``output_field`` of a ``RawSQL`` wrapper
    so Django's lookup machinery knows how to coerce the RHS value.
    """
    parts = chain.split("__")
    current_model = model
    fk_ref: str | None = None

    for i, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            msg = f"No field resolved before lookup suffix {'__'.join(parts[i:])!r} in {chain!r}"
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
            # If this is the final segment the user wrote `Q(fk_field=...)` —
            # use the FK column itself as the LHS with an exact lookup.
            if i == len(parts) - 1:
                return fk_ref, [], field.target_field  # type: ignore[union-attr]
            continue

        col = qn(field.column)  # type: ignore[union-attr]
        if fk_ref is None:
            sql = f"{row_ref}.{col}"
        else:
            tbl = qn(current_model._meta.db_table)  # noqa: SLF001
            pk = qn(current_model._meta.pk.column)  # noqa: SLF001
            sql = f"(SELECT {col} FROM {tbl} WHERE {pk} = {fk_ref})"
        return sql, parts[i + 1 :], field  # type: ignore[return-value]

    msg = f"Field chain {chain!r} could not be resolved"
    raise ValueError(msg)


def _inline_params(sql: str, params: Any) -> str:  # noqa: ANN401
    """Inline query parameters into SQL using schema_editor.quote_value."""
    if not params:
        return sql
    quoted = tuple(connection.schema_editor().quote_value(p) for p in params)
    return sql % quoted


def _compile_q(q: Q, model: type[Model], qn: Callable[[str], str], *, row_ref: str = "NEW") -> str:
    """Compile a Q object to PL/pgSQL using Django's lookup machinery.

    Supports every lookup and transform that Django itself supports,
    including custom lookups registered on field classes.  FK-traversing
    references (``product__stock``) compile to nested sub-selects on both
    sides of the comparison; local fields compile to ``{row_ref}."column"``.

    The LHS field's ``output_field`` drives the RHS coercion, so values like
    ``Decimal("10.50")`` or ``datetime(...)`` get Django's standard
    ``get_prep_value`` treatment rather than our own ad-hoc literal escaper.
    """
    query = Query(model=model, alias_cols=False)
    compiler = query.get_compiler(connection=connection)
    return _compile_q_node(q, model, qn, query, compiler, row_ref)


def _compile_q_node(  # noqa: PLR0913
    q: Q,
    model: type[Model],
    qn: Callable[[str], str],
    query: Query,
    compiler: Any,  # noqa: ANN401
    row_ref: str,
) -> str:
    parts: list[str] = []
    for child in q.children:
        if isinstance(child, Q):
            sub = _compile_q_node(child, model, qn, query, compiler, row_ref)
            parts.append(f"({sub})")
            continue

        key, value = child  # type: ignore[misc]
        lhs_sql, lookups, lhs_field = _resolve_lhs(key, model, qn, row_ref=row_ref)
        # RawSQL inputs here come from model metadata (column names via `qn`
        # and PK chains) — no user data. See `_resolve_lhs`.
        lhs_expr = RawSQL(lhs_sql, [], output_field=lhs_field)  # noqa: S611

        if isinstance(value, F):
            rhs_sql, _rem, rhs_field = _resolve_lhs(value.name, model, qn, row_ref=row_ref)  # type: ignore[attr-defined]
            rhs: Any = RawSQL(rhs_sql, [], output_field=rhs_field)  # noqa: S611
        else:
            rhs = value

        lookup = query.build_lookup(lookups or ["exact"], lhs_expr, rhs)
        sql, lookup_params = lookup.as_sql(compiler, connection)
        parts.append(_inline_params(sql, lookup_params))

    if not parts:
        return "TRUE"
    connector = f" {q.connector} "
    result = connector.join(parts)
    if q.negated:
        result = f"NOT ({result})"
    return result
