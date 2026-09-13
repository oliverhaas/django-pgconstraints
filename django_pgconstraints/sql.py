"""SQL helper functions for compiling Q objects to PL/pgSQL."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.models import F, Model, Q
from django.db.models.expressions import RawSQL
from django.db.models.sql import Query

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Field


@dataclass(frozen=True)
class _AggregateHop:
    """One reverse-FK hop in a multi-hop aggregate chain.

    Stored parent-to-leaf: ``chain[0]`` is the first hop from the parent
    (e.g. ``Customer.carts``) and ``chain[-1]`` is the leaf (e.g.
    ``Cart.items``). ``fk_column`` always names the FK column on
    *this* hop's table pointing back at the previous level.
    """

    rel_name: str  # accessor name on the parent model (e.g. "carts")
    fk_column: str  # FK column on this hop's table pointing to parent (e.g. "customer_id")
    table: str  # this hop's child table name (e.g. "testapp_cart")
    pk: str  # this hop's PK column (almost always "id")
    related_model: type[Model]  # the Django model class for this hop's table


def _col(field: Any) -> str:  # noqa: ANN401
    # django-stubs types Field.column as str | None because it's set by
    # contribute_to_class. It's always a concrete string by the time our
    # trigger helpers run.
    col = field.column
    if col is None:
        msg = f"{field!r} has no column"
        raise RuntimeError(msg)
    return col


# ------------------------------------------------------------------
# FK-chain field resolution
# ------------------------------------------------------------------


def _advance_fk(
    field: Any,  # noqa: ANN401
    current_model: type[Model],
    fk_ref: str | None,
    qn: Callable[[str], str],
    row_ref: str,
) -> tuple[str, Any]:
    """Advance one FK hop, returning the updated fk_ref and next model.

    First hop anchors on *row_ref* (``NEW."fk_id"``); subsequent hops wrap
    the previous fk_ref in a sub-select that walks from current_model to
    the next one.
    """
    fk_column = _col(field)
    if fk_ref is None:
        new_fk_ref = f"{row_ref}.{qn(fk_column)}" if row_ref else qn(fk_column)
    else:
        tbl = qn(current_model._meta.db_table)  # noqa: SLF001
        pk = qn(_col(current_model._meta.pk))  # noqa: SLF001
        new_fk_ref = f"(SELECT {qn(fk_column)} FROM {tbl} WHERE {pk} = {fk_ref})"
    return new_fk_ref, field.related_model


def _concrete_col_sql(
    field: Any,  # noqa: ANN401
    current_model: type[Model],
    fk_ref: str | None,
    qn: Callable[[str], str],
    row_ref: str,
) -> str:
    """Resolve a concrete field to SQL, wrapping in a sub-select if FK hops preceded it."""
    col = qn(_col(field))
    if fk_ref is None:
        return f"{row_ref}.{col}" if row_ref else col
    tbl = qn(current_model._meta.db_table)  # noqa: SLF001
    pk = qn(_col(current_model._meta.pk))  # noqa: SLF001
    return f"(SELECT {col} FROM {tbl} WHERE {pk} = {fk_ref})"


# Aggregate functions whose empty-set result we coerce to 0 instead of NULL.
# Mirrors Django's own coalescing in `Aggregate(default=...)` for typical cases.
_ZERO_DEFAULT_AGGREGATES = frozenset({"SUM", "COUNT"})


def _validate_aggregate_shape(aggregate: Any) -> str:  # noqa: ANN401
    """Reject filter, distinct, multi-source, and non-F sources. Return the F name."""
    if getattr(aggregate, "filter", None) is not None:
        msg = f"{type(aggregate).__name__}(filter=...) is not supported in GeneratedFieldTrigger expressions."
        raise NotImplementedError(msg)
    if getattr(aggregate, "distinct", False):
        msg = f"{type(aggregate).__name__}(distinct=True) is not supported in GeneratedFieldTrigger expressions."
        raise NotImplementedError(msg)

    # Aggregate.get_source_expressions() reserves trailing slots for filter
    # and default. Drop the Nones; we already rejected non-None filter above.
    sources = [s for s in aggregate.get_source_expressions() if s is not None]
    if len(sources) != 1:
        msg = (
            f"{type(aggregate).__name__} with {len(sources)} source expressions "
            f"is not supported in GeneratedFieldTrigger; pass a single F() reference."
        )
        raise NotImplementedError(msg)
    source = sources[0]
    if not isinstance(source, F):
        msg = f"Aggregate source must be an F() reference (or a string), got {type(source).__name__}."
        raise NotImplementedError(msg)
    return source.name  # type: ignore[attr-defined]


def _parse_aggregate_chain(
    name: str,
    parent_model: type[Model],
) -> tuple[list[_AggregateHop], type[Model], str | None]:
    """Walk the ``__``-separated path on *parent_model*, returning chain and leaf.

    Every non-final segment must be a reverse one-to-many FK accessor.
    The final segment is either:

    - a reverse FK accessor (the COUNT-over-relation case), in which case
      the leaf field is ``None`` and the chain extends through it;
    - a concrete column on the model reached by the previous hop, in
      which case the leaf field is that column name.

    Mixed forward/reverse paths and m2m hops are rejected with a
    ``ValueError`` whose message names the offending segment.
    """
    parts = name.split("__")
    chain: list[_AggregateHop] = []
    current_model = parent_model

    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        try:
            field = current_model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            owner = current_model.__name__
            msg = f"No relation or field {part!r} on {owner} (in aggregate path {name!r})"
            raise ValueError(msg) from None

        if getattr(field, "one_to_many", False):
            related_model: type[Model] = field.related_model  # type: ignore[assignment]
            fk_field = field.field  # type: ignore[union-attr]
            chain.append(
                _AggregateHop(
                    rel_name=part,
                    fk_column=_col(fk_field),
                    table=related_model._meta.db_table,  # noqa: SLF001
                    pk=_col(related_model._meta.pk),  # noqa: SLF001
                    related_model=related_model,
                ),
            )
            current_model = related_model
            continue

        if is_last and not field.is_relation:
            return chain, current_model, _col(field)

        owner = current_model.__name__
        msg = (
            f"Aggregate path {name!r}: segment {part!r} on {owner} must be a "
            f"reverse one-to-many relation"
            + (" (or a concrete column at the leaf)" if is_last else "")
            + f", got {type(field).__name__}."
        )
        raise ValueError(msg)

    if not chain:
        msg = f"Aggregate path {name!r} resolved to no hops; pass at least one reverse-relation accessor."
        raise ValueError(msg)
    # Path ended on a reverse-FK accessor — Count-over-relation case.
    return chain, current_model, None


def _walk_aggregate_chain_to_root(
    chain: tuple[_AggregateHop, ...],
    qn: Callable[[str], str],
    seed_sql: str,
) -> str:
    """Walk leaf-level FK values up through *chain* to the root parent's PK domain.

    *seed_sql* is a SELECT that returns leaf-level FK values (i.e. the
    column ``chain[-1].fk_column``, which names the next-level-up
    table's PKs). For each intermediate hop, wrap in a
    ``SELECT fk FROM <table> WHERE pk IN (...)``.

    For a single-hop chain the leaf hop's FK values *are* root-parent
    PKs, so the seed is returned unchanged.
    """
    inner = seed_sql
    for hop in reversed(chain[:-1]):
        inner = (
            f"SELECT DISTINCT {qn(hop.fk_column)} FROM {qn(hop.table)} "
            f"WHERE {qn(hop.fk_column)} IS NOT NULL "
            f"AND {qn(hop.pk)} IN ({inner})"
        )
    return inner


def _resolve_reverse_aggregate(
    aggregate: Any,  # noqa: ANN401
    parent_model: type[Model],
    qn: Callable[[str], str],
    *,
    row_ref: str,
) -> str:
    """Compile an Aggregate over an arbitrary chain of reverse FKs to a SQL subquery.

    ``Sum("lines__amount")`` on Invoice compiles to::

        COALESCE(
            (SELECT SUM("amount") FROM "testapp_invoiceline"
             WHERE "invoice_id" = NEW."id"),
            0
        )

    ``Sum("carts__items__amount")`` on Customer compiles to a nested form::

        COALESCE((
            SELECT SUM("amount") FROM "testapp_cartitem"
            WHERE "cart_id" IN (
                SELECT "id" FROM "testapp_cart" WHERE "customer_id" = NEW."id"
            )
        ), 0)

    Every hop must be a reverse one-to-many FK; mixed forward/reverse
    paths are rejected. ``filter=`` and ``distinct=True`` are also
    rejected until the trigger machinery handles them coherently.
    """
    name = _validate_aggregate_shape(aggregate)
    chain, leaf_model, leaf_field_column = _parse_aggregate_chain(name, parent_model)

    parent_pk_col = qn(_col(parent_model._meta.pk))  # noqa: SLF001
    func: str = aggregate.function  # SUM, COUNT, AVG, MAX, MIN
    agg_arg = "*" if leaf_field_column is None else qn(leaf_field_column)

    # Build inside-out: innermost WHERE compares the first hop's FK to the
    # parent's PK on row_ref; each subsequent hop wraps the current SELECT
    # in `IN (SELECT pk FROM ...)`.
    condition = f"{qn(chain[0].fk_column)} = {row_ref}.{parent_pk_col}"
    inner_table = chain[0].table
    inner_pk = chain[0].pk

    for hop in chain[1:]:
        inner_query = f"SELECT {qn(inner_pk)} FROM {qn(inner_table)} WHERE {condition}"
        condition = f"{qn(hop.fk_column)} IN ({inner_query})"
        inner_table = hop.table
        inner_pk = hop.pk

    leaf_table = qn(leaf_model._meta.db_table)  # noqa: SLF001
    aggregate_query = f"SELECT {func}({agg_arg}) FROM {leaf_table} WHERE {condition}"
    if func in _ZERO_DEFAULT_AGGREGATES:
        return f"COALESCE(({aggregate_query}), 0)"
    return f"({aggregate_query})"


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
            fk_ref, current_model = _advance_fk(field, current_model, fk_ref, qn, row_ref)
        else:
            # Concrete field found — remaining parts (if any) are the lookup.
            sql = _concrete_col_sql(field, current_model, fk_ref, qn, row_ref)
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
            fk_ref, current_model = _advance_fk(field, current_model, fk_ref, qn, row_ref)
            # If this is the final segment the user wrote `Q(fk_field=...)` —
            # use the FK column itself as the LHS with an exact lookup.
            if i == len(parts) - 1:
                return fk_ref, [], field.target_field  # type: ignore[union-attr]
            continue

        sql = _concrete_col_sql(field, current_model, fk_ref, qn, row_ref)
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
