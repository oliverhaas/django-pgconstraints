"""pgtrigger-based trigger classes for django-pgconstraints."""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import pgtrigger
import pgtrigger.utils
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connection
from django.db.models import Deferrable
from django.db.models.sql import Query

from django_pgconstraints.sql import (
    _AggregateHop,
    _col,
    _compile_q,
    _parse_aggregate_chain,
    _resolve_field_ref,
    _resolve_reverse_aggregate,
    _walk_aggregate_chain_to_root,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import Model, Q
    from django.db.models.expressions import BaseExpression


# pgtrigger caps trigger names at 47 chars; long aggregate chain paths
# (e.g. ``accounts__subscriptions__charges``) blow past that, so we
# fall back to a stable short hash when the human-readable form is too
# long. Reserve a few chars for the trailing ``_<hash>`` suffix and
# the surrounding ``{base}_agg_{op}_`` framing.
_MAX_TRIGGER_NAME_LENGTH = 47
_TRIGGER_NAME_HASH_LENGTH = 8


def _aggregate_trigger_name(base: str | None, op_name: str, rel_path: str) -> str:
    """Build a unique aggregate-trigger name that fits pgtrigger's length limit.

    Prefer the readable form ``{base}_agg_{op}_{rel_path}``; if that
    overflows, replace ``rel_path`` with a hash of the original path so
    the name stays stable across runs and unique per chain.
    """
    candidate = f"{base}_agg_{op_name}_{rel_path}"
    if len(candidate) <= _MAX_TRIGGER_NAME_LENGTH:
        return candidate
    digest = hashlib.md5(rel_path.encode(), usedforsecurity=False).hexdigest()[:_TRIGGER_NAME_HASH_LENGTH]
    return f"{base}_agg_{op_name}_{digest}"


# ======================================================================
# UniqueConstraintTrigger
# ======================================================================


def _compile_expression(expr: BaseExpression, model: type[Model], row_ref: str = "NEW") -> str:
    """Compile a Django expression to SQL, prefixing column refs with *row_ref*.

    Handles FK-traversal ``F()`` references (e.g. ``F("product__price")``)
    by resolving them to subqueries via ``_resolve_field_ref``, then compiling
    the rest of the expression normally through Django's expression compiler.
    """
    from django.db.models import F as DjangoF  # noqa: PLC0415
    from django.db.models.expressions import RawSQL  # noqa: PLC0415

    qn = pgtrigger.utils.quote

    # Replace FK-traversal F() refs with placeholder tokens, resolve them
    # separately, and stitch back after column-prefixing.
    placeholders: dict[str, str] = {}
    expr = _replace_fk_refs(expr, model, qn, row_ref, DjangoF, RawSQL, placeholders)

    query = Query(model=model, alias_cols=False)
    resolved = expr.resolve_expression(query, allow_joins=False, for_save=True)
    compiler = query.get_compiler(connection=connection)
    sql, params = resolved.as_sql(compiler, connection)
    if params:
        sql = sql % tuple(compiler.connection.schema_editor().quote_value(p) for p in params)

    # Prefix bare column references with the row reference.
    if row_ref:
        for field in model._meta.fields:  # noqa: SLF001
            quoted_col = f'"{field.column}"'
            sql = sql.replace(quoted_col, f"{row_ref}.{quoted_col}")

    # Restore FK-traversal subqueries (which already have correct refs).
    for placeholder, resolved_sql in placeholders.items():
        sql = sql.replace(placeholder, resolved_sql)

    return sql


def _replace_fk_refs(  # noqa: PLR0913
    expr: BaseExpression,
    model: type[Model],
    qn: Any,  # noqa: ANN401
    row_ref: str,
    f_class: type,
    rawsql_class: type,
    placeholders: dict[str, str],
) -> BaseExpression:
    """Replace FK-traversal F() refs and reverse-relation Aggregates with RawSQL placeholders.

    Aggregates are handled as a unit (we don't recurse into their source
    expressions) because their F() refs name reverse relations rather than
    forward FK chains, so they need a different SQL shape.
    """
    from django.db.models import Aggregate  # noqa: PLC0415

    if isinstance(expr, Aggregate):
        resolved_sql = _resolve_reverse_aggregate(expr, model, qn, row_ref=row_ref)
        token = f"__pgc_agg_{len(placeholders)}__"
        placeholders[token] = resolved_sql
        return rawsql_class(token, ())

    if isinstance(expr, f_class):
        name: str = expr.name  # type: ignore[attr-defined]
        if "__" in name:
            resolved_sql, _ = _resolve_field_ref(name, model, qn, row_ref=row_ref)
            token = f"__pgc_fk_{len(placeholders)}__"
            placeholders[token] = resolved_sql
            return rawsql_class(token, ())
        return expr

    # Clone the expression and recurse into source_expressions.
    clone = expr.copy()
    source_exprs = clone.get_source_expressions()
    if source_exprs:
        new_sources = [
            _replace_fk_refs(child, model, qn, row_ref, f_class, rawsql_class, placeholders)
            if child is not None
            else None
            for child in source_exprs
        ]
        clone.set_source_expressions(new_sources)  # type: ignore[arg-type]
    return clone


class UniqueConstraintTrigger(pgtrigger.Trigger):
    """Enforce uniqueness of field values, with FK-traversal and expression support.

    *fields* can contain:
    - plain field names (``"slug"``)
    - ``__``-separated FK chains (``"book__author"``)

    *expressions* can contain Django expressions (``Lower("email")``).

    At least one of *fields* or *expressions* must be provided.

    Set ``deferrable=Deferrable.DEFERRED`` for a constraint trigger that
    fires at commit time (default ``None`` — fires immediately).

    **Index backing (``index=True``):**

    When ``index`` is set to True, a matching ``CREATE UNIQUE INDEX`` is
    installed alongside the trigger. The index makes uniqueness O(log n)
    per insert (vs. the trigger's O(n) ``EXISTS`` scan), lets the query
    planner use the column set for ordinary SELECT queries, and rejects
    duplicates at the PG catalog level before the trigger fires. The
    trigger stays installed as a second layer of defense and for
    :meth:`validate` / Django ``full_clean`` support.

    Supported index-backed configurations:

    - Plain fields: ``fields=["slug"]``
    - Composite: ``fields=["slug", "section"]``
    - Functional: ``Lower("slug")`` as an expression
    - Partial: ``fields=["slug"], condition=Q(published=True)``
    - NULLS NOT DISTINCT (PG 15+): ``fields=["slug"], nulls_distinct=False``

    Not supported with ``index=True`` (raises ``ValueError`` at
    construction):

    - FK-traversal ``__`` in ``fields`` — unique indexes only cover
      same-table columns.
    - FK-traversal ``F()`` references in expressions — same reason.
    - ``deferrable=Deferrable.DEFERRED`` — PG unique indexes cannot be
      deferred.

    **Trade-off:** when ``index=True`` rejects a duplicate, the error
    comes from PostgreSQL's native index-level message, not the
    trigger's ``violation_error_message``. If you need the custom error
    message, use ``index=False`` and rely on the trigger alone.
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
        index: bool = False,
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
        if expressions and deferrable == Deferrable.DEFERRED:
            msg = "UniqueConstraintTrigger with expressions cannot be deferred."
            raise ValueError(msg)

        if index:
            self._validate_indexable(fields, expressions, deferrable)

        self.fields = list(fields)
        self.expressions = list(expressions)
        self.unique_condition = condition
        self.nulls_distinct = nulls_distinct
        self.index = index

        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message

        if deferrable == Deferrable.DEFERRED:
            kwargs.setdefault("timing", pgtrigger.Deferred)
        elif deferrable == Deferrable.IMMEDIATE:
            kwargs.setdefault("timing", pgtrigger.Immediate)

        super().__init__(**kwargs)

    @staticmethod
    def _validate_indexable(
        fields: list[str] | tuple[str, ...],
        expressions: tuple[BaseExpression, ...],
        deferrable: Deferrable | None,
    ) -> None:
        """Raise ValueError if the config cannot be backed by a CREATE UNIQUE INDEX.

        Index backing requires all column references to live on the trigger's
        own table — FK traversal requires a subquery, which PG cannot put
        into a unique index. Deferred unique indexes are not supported at
        all by PG.
        """
        if deferrable == Deferrable.DEFERRED:
            msg = (
                "UniqueConstraintTrigger(index=True) cannot be combined with "
                "deferrable=Deferrable.DEFERRED: PostgreSQL unique indexes "
                "cannot be deferred. Use index=False to keep the trigger-only path."
            )
            raise ValueError(msg)

        for chain in fields:
            if "__" in chain:
                msg = (
                    f"UniqueConstraintTrigger(index=True) cannot be combined with "
                    f"FK traversal in fields: {chain!r} contains '__'. "
                    f"Unique indexes only support same-table columns."
                )
                raise ValueError(msg)

        # Check expressions for FK-traversal F() references.
        from django.db.models import F as DjangoF  # noqa: PLC0415

        def _has_fk_traversal(expr: Any) -> bool:  # noqa: ANN401
            if isinstance(expr, DjangoF) and "__" in expr.name:  # type: ignore[attr-defined]
                return True
            if hasattr(expr, "get_source_expressions"):
                return any(_has_fk_traversal(c) for c in expr.get_source_expressions() if c is not None)
            return False

        for expr in expressions:
            if _has_fk_traversal(expr):
                msg = (
                    "UniqueConstraintTrigger(index=True) cannot be combined with "
                    "FK traversal in expressions: an F() reference contains '__'. "
                    "Unique indexes only support same-table columns."
                )
                raise ValueError(msg)

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
            condition_sql = _compile_q(self.unique_condition, model, qn, row_ref="NEW")  # type: ignore[arg-type]

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
                        'Unique constraint "{self.name}" is violated.'
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

    def install(self, model: Model, database: str | None = None) -> None:
        """Install the trigger, plus the backing unique index if ``index=True``."""
        super().install(model, database=database)
        if self.index:
            self._install_index(model, database=database)  # type: ignore[arg-type]

    def uninstall(self, model: Model, database: str | None = None) -> None:
        """Uninstall the trigger, and drop the backing unique index if ``index=True``."""
        if self.index:
            self._uninstall_index(model, database=database)  # type: ignore[arg-type]
        super().uninstall(model, database=database)

    def _index_name(self) -> str:
        """Derive a deterministic index name from the trigger name.

        PostgreSQL identifiers are limited to 63 bytes; if the trigger name
        is already long this may collide with the limit. In that case we fall
        back to hashing the name, similar to how pgtrigger derives stable
        identifiers for long names.
        """
        name = self.name or ""
        base = f"pgconstraints_idx_{name}"
        if len(base) <= 63:  # noqa: PLR2004
            return base
        import hashlib  # noqa: PLC0415

        digest = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()[:8]
        # Truncate the prefix to leave room for the hash suffix.
        return f"pgconstraints_idx_{name[: 63 - 18 - 9]}_{digest}"

    def _build_index_definition(self, model: type[Model]) -> str:
        """Build the CREATE UNIQUE INDEX SQL.

        Only called when ``self.index`` is True, which means the config has
        already been validated as indexable (no FK traversal, no deferred).
        """
        qn = pgtrigger.utils.quote
        table = qn(model._meta.db_table)  # noqa: SLF001
        idx_name = qn(self._index_name())

        # Columns or expressions
        if self.fields:
            cols = ", ".join(
                qn(model._meta.get_field(f).column)  # type: ignore[union-attr]  # noqa: SLF001
                for f in self.fields
            )
        else:
            # Expressions compile without a row_ref prefix (index context).
            cols = ", ".join(_compile_expression(expr, model, row_ref="") for expr in self.expressions)

        nulls_clause = " NULLS NOT DISTINCT" if self.nulls_distinct is False else ""

        where_clause = ""
        if self.unique_condition is not None:
            # _compile_q with row_ref="" produces bare column references,
            # usable as a WHERE clause in a CREATE INDEX.
            cond_sql = _compile_q(self.unique_condition, model, qn, row_ref="")
            where_clause = f" WHERE {cond_sql}"

        return f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols}){nulls_clause}{where_clause}"

    def _install_index(self, model: type[Model], database: str | None = None) -> None:
        from django.db import DEFAULT_DB_ALIAS, connections  # noqa: PLC0415

        sql = self._build_index_definition(model)
        conn = connections[database or DEFAULT_DB_ALIAS]
        with conn.cursor() as cur:
            cur.execute(sql)

    def _uninstall_index(self, model: type[Model], database: str | None = None) -> None:  # noqa: ARG002
        from django.db import DEFAULT_DB_ALIAS, connections  # noqa: PLC0415

        qn = pgtrigger.utils.quote
        idx_name = qn(self._index_name())
        conn = connections[database or DEFAULT_DB_ALIAS]
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {idx_name}")


# ======================================================================
# CheckConstraintTrigger
# ======================================================================


class CheckConstraintTrigger(pgtrigger.Trigger):
    """Enforce a check condition, with FK-traversal support.

    Supports cross-table ``F()`` expressions in the condition, e.g.
    ``Q(quantity__lte=F("product__stock"))``.  Uses the same ``condition``
    parameter name as Django's ``CheckConstraint``.

    For same-table conditions, ``validate()`` performs Python-level
    validation via ``Q.check()``.  FK-traversal conditions skip Python
    validation and rely on the database trigger.
    """

    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    violation_error_code: str | None = None
    violation_error_message: str = 'Constraint "%(name)s" is violated.'

    def __init__(
        self,
        *,
        condition: Q,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if not getattr(condition, "conditional", False):
            msg = "CheckConstraintTrigger.condition must be a Q instance or boolean expression."
            raise TypeError(msg)
        self.check_condition = condition
        if violation_error_code is not None:
            self.violation_error_code = violation_error_code
        if violation_error_message is not None:
            self.violation_error_message = violation_error_message
        super().__init__(**kwargs)

    def get_violation_error_message(self) -> str:
        return self.violation_error_message % {"name": self.name}

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote
        check_sql = _compile_q(self.check_condition, model, qn, row_ref="NEW")  # type: ignore[arg-type]

        return self.format_sql(f"""
            IF NOT ({check_sql}) THEN
                RAISE EXCEPTION
                    'Check constraint "{self.name}" is violated.'
                    USING ERRCODE = '23514', CONSTRAINT = '{self.name}';
            END IF;
            RETURN NEW;
        """)

    def _has_fk_refs(self) -> bool:
        """Check if condition contains FK-traversal F() references."""
        from django.db.models import F as DjangoF  # noqa: PLC0415
        from django.db.models import Q as DjangoQ  # noqa: PLC0415

        def _walk(node: Any) -> bool:  # noqa: ANN401
            if isinstance(node, DjangoF):
                return "__" in node.name  # type: ignore[attr-defined]
            if isinstance(node, DjangoQ):
                for child in node.children:
                    if isinstance(child, (DjangoQ, DjangoF)):
                        if _walk(child):
                            return True
                    elif isinstance(child, tuple) and len(child) == 2:  # noqa: PLR2004
                        _, value = child
                        if isinstance(value, DjangoF) and "__" in value.name:  # type: ignore[attr-defined]
                            return True
            return False

        return _walk(self.check_condition)

    def validate(
        self,
        model: type[Model],
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        """Python-level validation, compatible with Django's full_clean().

        For same-table conditions, uses ``Q.check()`` against instance field
        values.  FK-traversal conditions are skipped (trigger is primary
        enforcement).
        """
        if self._has_fk_refs():
            return

        from django.db.models import Q as DjangoQ  # noqa: PLC0415

        # Mirrors Django 6.0's CheckConstraint.validate (db/models/constraints.py).
        # Re-verify the call signature on Django minor-version upgrades.
        against = instance._get_field_expression_map(meta=model._meta, exclude=exclude)  # type: ignore[attr-defined]  # noqa: SLF001

        if not DjangoQ(self.check_condition).check(against, using=using):
            raise ValidationError(
                self.get_violation_error_message(),
                code=self.violation_error_code,
            )


# ======================================================================
# GeneratedFieldTrigger
# ======================================================================


def _find_fk_refs(expr: BaseExpression) -> list[str]:
    """Find all FK-traversal F() references in an expression tree.

    Returns a list of ``__``-separated field chains (e.g. ``["part__base_price"]``).

    Aggregate subtrees are skipped: their F() refs name reverse relations
    rather than forward FK chains and are routed through a separate
    discovery path.
    """
    from django.db.models import Aggregate  # noqa: PLC0415
    from django.db.models import F as DjangoF  # noqa: PLC0415

    refs: list[str] = []
    if isinstance(expr, Aggregate):
        return refs
    if isinstance(expr, DjangoF):
        name: str = expr.name
        if "__" in name:
            refs.append(name)
    else:
        for child in expr.get_source_expressions():
            if child is not None:
                refs.extend(_find_fk_refs(child))
    return refs


def _find_aggregate_refs(expr: BaseExpression) -> list[tuple[Any, str]]:
    """Return ``(aggregate, source_name)`` pairs for every Aggregate in the tree.

    Aggregates are not recursed into; their source expressions are validated
    by :func:`_resolve_reverse_aggregate` at compile time. ``F()`` references
    are leaves with no source expressions.
    """
    from django.db.models import Aggregate  # noqa: PLC0415
    from django.db.models import F as DjangoF  # noqa: PLC0415

    refs: list[tuple[Any, str]] = []
    if isinstance(expr, Aggregate):
        sources = [s for s in expr.get_source_expressions() if s is not None]
        if len(sources) == 1 and isinstance(sources[0], DjangoF):
            f_source: Any = sources[0]
            refs.append((expr, f_source.name))
        return refs
    if isinstance(expr, DjangoF):
        return refs
    for child in expr.get_source_expressions():
        if child is not None:
            refs.extend(_find_aggregate_refs(child))
    return refs


@dataclass
class _FKHop:
    """One hop in an FK chain."""

    fk_field_name: str  # field name on the current model (e.g. "part")
    fk_column: str  # DB column name (e.g. "part_id")
    related_model: type[Model]  # the model on the other end


def _parse_fk_chain(chain: str, model: type[Model]) -> tuple[list[_FKHop], str]:
    """Parse an FK chain into hops and the leaf field name.

    For ``"part__supplier__markup_pct"`` on PurchaseItem returns:
    - hops = [_FKHop("part", "part_id", Part), _FKHop("supplier", "supplier_id", Supplier)]
    - leaf_field = "markup_pct"
    """
    parts = chain.split("__")
    hops: list[_FKHop] = []
    current_model = model

    for part in parts[:-1]:
        fk_field = current_model._meta.get_field(part)  # noqa: SLF001
        hops.append(
            _FKHop(
                fk_field_name=part,
                fk_column=_col(fk_field),
                related_model=fk_field.related_model,  # type: ignore[arg-type]
            ),
        )
        current_model = fk_field.related_model  # type: ignore[assignment]

    return hops, parts[-1]


def _build_chain_back_where(
    chain_back: list[dict[str, str]],
    qn: Callable[[str], str],
    leaf_sql: str,
) -> str:
    """Build the WHERE clause that walks chain_back to a caller-supplied leaf.

    ``chain_back`` is ordered ``child → ... → root``. Each entry describes
    one hop: ``{fk_col (on this table), table (this model's table),
    pk (this model's PK)}``.

    ``leaf_sql`` is a complete SELECT that returns root-model PKs — the
    rows whose state change should drive the cascade. Callers construct
    this differently:

    - ``_GeneratedFieldReverse.get_func`` builds a ``SELECT n.pk FROM
      new_rows n JOIN old_rows o ON n.pk = o.pk WHERE n.watched IS
      DISTINCT FROM o.watched`` so only rows whose watched column
      actually changed are cascaded.

    - ``refresh_dependent(queryset)`` builds a ``SELECT pk FROM root_table
      WHERE <queryset's WHERE clause>`` so only rows the caller asked for
      are reconciled.

    Single-hop result:
        "<first_fk_col>" IN (<leaf_sql>)

    Multi-hop result (walking outward from ``root`` toward ``child``):
        "<first_fk_col>" IN (
            SELECT "<hop[1].pk>" FROM "<hop[1].table>"
            WHERE "<hop[1].fk_col>" IN (... IN (<leaf_sql>))
        )
    """
    if len(chain_back) == 1:
        hop = chain_back[0]
        return f"{qn(hop['fk_col'])} IN ({leaf_sql})"

    last = chain_back[-1]
    inner = f"SELECT {qn(last['pk'])} FROM {qn(last['table'])} WHERE {qn(last['fk_col'])} IN ({leaf_sql})"

    for hop in reversed(chain_back[1:-1]):
        inner = f"SELECT {qn(hop['pk'])} FROM {qn(hop['table'])} WHERE {qn(hop['fk_col'])} IN ({inner})"

    first = chain_back[0]
    return f"{qn(first['fk_col'])} IN ({inner})"


class _GeneratedFieldReverse(pgtrigger.Trigger):
    """AFTER UPDATE trigger on a related model that recomputes the child's generated field.

    Statement-level: fires once per UPDATE statement on the related model,
    uses REFERENCING transition tables to coalesce bulk cascades into a
    single set-based UPDATE. The IS DISTINCT FROM gate moves from a
    per-row IF guard to a join between new_rows and old_rows in the WHERE
    clause, so bulk updates that don't actually change the watched column
    incur zero child-row touches.
    """

    when = pgtrigger.After
    level = pgtrigger.Statement
    referencing = pgtrigger.Referencing(old="old_rows", new="new_rows")

    def __init__(
        self,
        *,
        child_model_label: str,
        child_field: str,
        expression: BaseExpression,
        trigger_field: str,
        # The FK chain from child model to the trigger model, used to build
        # the WHERE clause that finds affected child rows.
        chain_back: list[dict[str, str]],
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.child_model_label = child_model_label
        self.child_field = child_field
        self.expression = expression
        self.trigger_field = trigger_field
        self.chain_back = chain_back
        # Statement-level triggers with REFERENCING transition tables cannot
        # use UPDATE OF <column> in PostgreSQL ("transition tables cannot be
        # specified for triggers with column lists"), so we listen to all
        # UPDATEs on the related model. The IS DISTINCT FROM join between
        # new_rows and old_rows in the WHERE clause filters out statements
        # that don't actually change the watched column, preserving the
        # no-op gate semantics from the row-level implementation.
        kwargs.setdefault("operation", pgtrigger.Update)
        super().__init__(**kwargs)

    def get_func(self, model: Model) -> str:
        from django.apps import apps  # noqa: PLC0415

        qn = pgtrigger.utils.quote

        child_model = apps.get_model(self.child_model_label)
        child_table = qn(child_model._meta.db_table)  # noqa: SLF001
        child_target_col = qn(child_model._meta.get_field(self.child_field).column)  # noqa: SLF001
        trigger_pk_col = qn(model._meta.pk.column)  # noqa: SLF001
        watched_col = qn(self.trigger_field)

        # Compile the expression for the UPDATE context (child-row refs use
        # the child table name as the row reference).
        expr_sql = _compile_expression(self.expression, child_model, row_ref=child_table)

        leaf_sql = (
            f"SELECT n.{trigger_pk_col} FROM new_rows n "
            f"JOIN old_rows o ON n.{trigger_pk_col} = o.{trigger_pk_col} "
            f"WHERE n.{watched_col} IS DISTINCT FROM o.{watched_col}"
        )
        where = _build_chain_back_where(self.chain_back, qn, leaf_sql)

        return self.format_sql(f"""
            UPDATE {child_table}
            SET {child_target_col} = {expr_sql}
            WHERE {where};
            RETURN NULL;
        """)


class _GeneratedFieldAggregateReverse(pgtrigger.Trigger):
    """Statement-level AFTER trigger on the child of an aggregated reverse FK.

    Recomputes the parent's aggregate field for every parent whose child
    rows changed in the firing statement. One instance handles a single
    operation (INSERT, UPDATE, or DELETE); the multi-operation case is
    served by registering separate instances.

    Affected parent IDs come from the transition tables: ``new_rows`` for
    INSERT, ``old_rows`` for DELETE, and the union of both for UPDATE
    (a row whose FK pivots between parents touches both).
    """

    when = pgtrigger.After
    level = pgtrigger.Statement

    _OPERATION_MAP: ClassVar[dict[str, Any]] = {
        "insert": pgtrigger.Insert,
        "update": pgtrigger.Update,
        "delete": pgtrigger.Delete,
    }

    def __init__(  # noqa: PLR0913
        self,
        *,
        parent_model_label: str,
        parent_field: str,
        expression: BaseExpression,
        chain: tuple[_AggregateHop, ...],
        operation_name: str,
        aggregated_columns: tuple[str, ...] = (),
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        if operation_name not in self._OPERATION_MAP:
            msg = f"Unsupported operation_name: {operation_name!r}"
            raise ValueError(msg)
        if not chain:
            msg = "chain must contain at least one hop"
            raise ValueError(msg)
        self.parent_model_label = parent_model_label
        self.parent_field = parent_field
        self.expression = expression
        self.chain = chain
        self.operation_name = operation_name
        self.aggregated_columns = aggregated_columns

        kwargs.setdefault("operation", self._OPERATION_MAP[operation_name])
        if operation_name == "insert":
            kwargs.setdefault("referencing", pgtrigger.Referencing(new="new_rows"))
        elif operation_name == "delete":
            kwargs.setdefault("referencing", pgtrigger.Referencing(old="old_rows"))
        else:  # update
            kwargs.setdefault("referencing", pgtrigger.Referencing(old="old_rows", new="new_rows"))
        super().__init__(**kwargs)

    @property
    def leaf_fk_column(self) -> str:
        """FK column on the leaf table that points up to the next-level table."""
        return self.chain[-1].fk_column

    def get_func(self, model: Model) -> str:
        from django.apps import apps  # noqa: PLC0415

        qn = pgtrigger.utils.quote

        parent_model = apps.get_model(self.parent_model_label)
        parent_table = qn(parent_model._meta.db_table)  # noqa: SLF001
        parent_pk_col = qn(_col(parent_model._meta.pk))  # noqa: SLF001
        parent_field_col = qn(_col(parent_model._meta.get_field(self.parent_field)))  # noqa: SLF001

        # Compile the parent's aggregate expression with the parent table
        # as row_ref so the (possibly nested) child subquery is correlated
        # to the outer UPDATE on the parent.
        expr_sql = _compile_expression(self.expression, parent_model, row_ref=parent_table)

        if self.operation_name == "insert":
            affected = self._build_simple_walk(qn, "new_rows")
        elif self.operation_name == "delete":
            affected = self._build_simple_walk(qn, "old_rows")
        else:  # update
            child_pk_col = qn(_col(model._meta.pk))  # noqa: SLF001
            affected = self._build_update_affected_sql(qn, child_pk_col)

        return self.format_sql(f"""
            UPDATE {parent_table}
            SET {parent_field_col} = {expr_sql}
            WHERE {parent_pk_col} IN ({affected});
            RETURN NULL;
        """)

    def _build_simple_walk(self, qn: Callable[[str], str], transition_table: str) -> str:
        """Walk from a transition table's leaf-FK column up to root parent PKs."""
        leaf_fk = qn(self.leaf_fk_column)
        seed = f"SELECT DISTINCT {leaf_fk} FROM {transition_table} WHERE {leaf_fk} IS NOT NULL"
        return _walk_aggregate_chain_to_root(self.chain, qn, seed)

    def _build_update_affected_sql(
        self,
        qn: Callable[[str], str],
        child_pk_col: str,
    ) -> str:
        """Affected root parent PKs for an UPDATE on the leaf table.

        - **New side**: rows where any watched column changed contribute
          their post-update leaf FK, walked up the chain. Catches same-
          parent edits and the destination of an FK pivot.
        - **Old side**: rows where the leaf FK pivoted contribute their
          pre-update leaf FK, walked up the chain. Catches the source
          parent of a pivot.

        Rows that touch neither the leaf FK nor any aggregated column
        contribute nothing; the parent UPDATE skips entirely.
        """
        leaf_fk = qn(self.leaf_fk_column)
        watched = [
            f"new_rows.{leaf_fk} IS DISTINCT FROM old_rows.{leaf_fk}",
            *[f"new_rows.{qn(col)} IS DISTINCT FROM old_rows.{qn(col)}" for col in self.aggregated_columns],
        ]
        any_watched_changed = " OR ".join(watched)
        fk_pivoted = f"new_rows.{leaf_fk} IS DISTINCT FROM old_rows.{leaf_fk}"
        join = f"new_rows JOIN old_rows ON new_rows.{child_pk_col} = old_rows.{child_pk_col}"

        new_seed = (
            f"SELECT DISTINCT new_rows.{leaf_fk} FROM {join} "
            f"WHERE new_rows.{leaf_fk} IS NOT NULL AND ({any_watched_changed})"
        )
        old_seed = (
            f"SELECT DISTINCT old_rows.{leaf_fk} FROM {join} WHERE old_rows.{leaf_fk} IS NOT NULL AND ({fk_pivoted})"
        )
        new_side = _walk_aggregate_chain_to_root(self.chain, qn, new_seed)
        old_side = _walk_aggregate_chain_to_root(self.chain, qn, old_seed)
        return f"{new_side} UNION {old_side}"


class GeneratedFieldTrigger(pgtrigger.Trigger):
    """Automatically compute and set a field value from an expression.

    Trigger-based replacement for Django's ``GeneratedField`` that fires
    ``BEFORE INSERT OR UPDATE`` and sets ``NEW.<field>`` to the resolved
    expression value.  Supports FK-traversal via ``__`` notation
    (e.g. ``F("product__price")``) and automatically creates reverse
    triggers on related models to recompute when referenced data changes.

    **Important differences from GeneratedField:**

    - The target field must be defined manually on the model (e.g.
      ``total = models.DecimalField(default=0)``). There is no
      ``output_field`` parameter.
    - The field is **read-only by convention**: any manual write (via ORM
      or raw SQL) is silently overwritten by the trigger on the next
      INSERT or UPDATE.  The computed value always wins.

    **In-memory refresh:**

    By default (``auto_refresh=True``), ``save()`` and ``bulk_create()``
    piggyback a ``RETURNING`` clause on the statement Django already
    issues, so the trigger-computed value is written back onto the
    Python instance without a separate query.  Pass
    ``auto_refresh=False`` to skip this and call
    ``instance.refresh_from_db()`` yourself.

    ``QuerySet.update()`` and ``bulk_update()`` do not refresh passed-in
    instances — those callers must use :func:`~django_pgconstraints.refresh_dependent`
    or ``refresh_from_db()`` as needed.

    **Cascading behavior:**

    When a referenced field on a related model changes, an
    ``AFTER UPDATE`` statement-level trigger automatically recomputes
    this field on every child row. Bulk updates
    (``Model.objects.filter(...).update(...)``) cascade in one set-based
    ``UPDATE`` per dependency, not one per affected parent row.

    If you bypass triggers (raw SQL, ``ALTER TABLE ... DISABLE TRIGGER``,
    restoring a dump without triggers attached), call
    :func:`~django_pgconstraints.refresh_dependent` with a queryset of
    the parent model to reconcile dependent fields::

        from django_pgconstraints import refresh_dependent
        refresh_dependent(Supplier.objects.filter(pk__in=changed_ids))

    **Admin integration:**

    Mix :class:`~django_pgconstraints.ComputedFieldsReadOnlyAdminMixin`
    into your ``ModelAdmin`` to prevent users from typing into computed
    fields in admin (they would be silently overwritten on save).
    """

    when = pgtrigger.Before
    operation = pgtrigger.Insert | pgtrigger.Update

    def __init__(
        self,
        *,
        field: str,
        expression: BaseExpression,
        auto_refresh: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self.field = field
        self.expression = expression
        self.auto_refresh = auto_refresh
        super().__init__(**kwargs)

    def get_func(self, model: Model) -> str:
        qn = pgtrigger.utils.quote
        target_col = qn(model._meta.get_field(self.field).column)  # type: ignore[union-attr]  # noqa: SLF001
        expr_sql = _compile_expression(self.expression, model, row_ref="NEW")  # type: ignore[arg-type]

        return self.format_sql(f"""
            NEW.{target_col} := {expr_sql};
            RETURN NEW;
        """)

    def get_reverse_triggers(self, model: type[Model]) -> list[tuple[type[Model], pgtrigger.Trigger]]:  # noqa: C901
        """Return (related_model, trigger) pairs for reverse triggers.

        For each FK chain in the expression, creates reverse triggers on:
        1. The leaf model (when the referenced field changes)
        2. Each intermediate model (when the FK column changes)

        For each Aggregate over a reverse relation, creates statement-level
        triggers on the child model so child INSERT/UPDATE/DELETE refresh
        the parent's aggregate.
        """
        fk_refs = _find_fk_refs(self.expression)
        aggregate_refs = _find_aggregate_refs(self.expression)
        if not fk_refs and not aggregate_refs:
            return []

        result: list[tuple[type[Model], pgtrigger.Trigger]] = []
        seen: set[str] = set()
        child_label = model._meta.label  # noqa: SLF001

        for chain in fk_refs:
            hops, leaf_field = _parse_fk_chain(chain, model)

            # Build the chain_back data for WHERE clause construction.
            # Each entry: {fk_col on this model, this model's table, this model's pk}
            chain_back_all: list[dict[str, str]] = []
            current = model
            for hop in hops:
                chain_back_all.append(
                    {
                        "fk_col": hop.fk_column,
                        "table": current._meta.db_table,  # noqa: SLF001
                        "pk": _col(current._meta.pk),  # noqa: SLF001
                    },
                )
                current = hop.related_model

            # 1. Leaf model trigger: fires when the actual field changes.
            leaf_model = hops[-1].related_model
            leaf_key = f"{leaf_model._meta.label}.{leaf_field}"  # noqa: SLF001
            if leaf_key not in seen:
                seen.add(leaf_key)
                name_suffix = "__".join(h.fk_field_name for h in hops)
                leaf_col = _col(leaf_model._meta.get_field(leaf_field))  # noqa: SLF001
                result.append(
                    (
                        leaf_model,
                        _GeneratedFieldReverse(
                            child_model_label=child_label,
                            child_field=self.field,
                            expression=self.expression,
                            trigger_field=leaf_col,
                            chain_back=chain_back_all,
                            name=f"{self.name}_rev_{name_suffix}",
                        ),
                    ),
                )

            # 2. Intermediate model triggers: fire when the FK column changes.
            # E.g. for chain part__supplier__markup_pct, when Part.supplier_id changes.
            for i in range(len(hops) - 1):
                trigger_model = hops[i].related_model
                # The FK column on trigger_model that points to the next hop.
                fk_col_name = hops[i + 1].fk_column
                fk_field_name = hops[i + 1].fk_field_name
                inter_key = f"{trigger_model._meta.label}.{fk_field_name}"  # noqa: SLF001
                if inter_key not in seen:
                    seen.add(inter_key)
                    result.append(
                        (
                            trigger_model,
                            _GeneratedFieldReverse(
                                child_model_label=child_label,
                                child_field=self.field,
                                expression=self.expression,
                                trigger_field=fk_col_name,
                                chain_back=chain_back_all[: i + 1],
                                name=f"{self.name}_rev_{hops[i].fk_field_name}",
                            ),
                        ),
                    )

        # Aggregate reverse triggers: one set per unique chain. Multiple
        # aggregates over the same chain (e.g. Sum + Count over the same
        # leaf relation) share triggers; the UPDATE gating clause covers
        # the union of their aggregated columns.
        by_chain: dict[tuple[_AggregateHop, ...], dict[str, Any]] = {}
        for _aggregate, source_name in aggregate_refs:
            agg_chain, agg_leaf_model, agg_leaf_field = _parse_aggregate_chain(source_name, model)
            agg_chain_key = tuple(agg_chain)
            entry = by_chain.setdefault(
                agg_chain_key,
                {"leaf_model": agg_leaf_model, "leaf_fields": []},
            )
            entry["leaf_fields"].append(agg_leaf_field)

        for agg_chain_key, group in by_chain.items():
            agg_leaf_model = group["leaf_model"]
            aggregated_columns = tuple(
                sorted({lf for lf in group["leaf_fields"] if lf is not None}),
            )
            rel_path = "__".join(hop.rel_name for hop in agg_chain_key)

            # Leaf-table triggers (INSERT, UPDATE with full gating, DELETE).
            for op_name in ("insert", "update", "delete"):
                result.append(  # noqa: PERF401
                    (
                        agg_leaf_model,
                        _GeneratedFieldAggregateReverse(
                            parent_model_label=child_label,
                            parent_field=self.field,
                            expression=self.expression,
                            chain=agg_chain_key,
                            operation_name=op_name,
                            aggregated_columns=aggregated_columns,
                            name=_aggregate_trigger_name(self.name, op_name, rel_path),
                        ),
                    ),
                )

            # Intermediate-table triggers: every non-leaf hop needs UPDATE
            # (FK pivot to a different ancestor) and DELETE (its descendants
            # cascade out from under us; the leaf-table DELETE trigger can't
            # walk back through a row that was just removed). INSERT is
            # skipped: a freshly-inserted intermediate has no descendants,
            # so it can't change the aggregate yet.
            for i in range(len(agg_chain_key) - 1):
                sub_chain = agg_chain_key[: i + 1]
                intermediate_model = agg_chain_key[i].related_model
                sub_rel_path = "__".join(hop.rel_name for hop in sub_chain)
                for op_name in ("update", "delete"):
                    result.append(  # noqa: PERF401
                        (
                            intermediate_model,
                            _GeneratedFieldAggregateReverse(
                                parent_model_label=child_label,
                                parent_field=self.field,
                                expression=self.expression,
                                chain=sub_chain,
                                operation_name=op_name,
                                # Aggregated columns live on the *leaf*; an
                                # intermediate row never carries them.
                                aggregated_columns=(),
                                name=_aggregate_trigger_name(self.name, op_name, sub_rel_path),
                            ),
                        ),
                    )

        return result

    def install(self, model: Model, database: str | None = None) -> None:
        """Install forward trigger + any reverse triggers on related models."""
        super().install(model, database=database)
        for related_model, trigger in self.get_reverse_triggers(model):  # type: ignore[arg-type]
            trigger.install(related_model, database=database)  # type: ignore[arg-type]

    def uninstall(self, model: Model, database: str | None = None) -> None:
        """Uninstall forward trigger + any reverse triggers."""
        super().uninstall(model, database=database)
        for related_model, trigger in self.get_reverse_triggers(model):  # type: ignore[arg-type]
            trigger.uninstall(related_model, database=database)  # type: ignore[arg-type]
