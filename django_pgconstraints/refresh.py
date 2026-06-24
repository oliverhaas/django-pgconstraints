"""Public refresh_dependent helper.

Walks the GeneratedFieldTrigger registry at call time and issues one
targeted recompute UPDATE per dependent child model / target-field pair.
Used to reconcile computed fields after a bulk load or raw-SQL operation
that bypassed the BEFORE INSERT/UPDATE forward trigger.
"""

from typing import TYPE_CHECKING, Any

import pgtrigger
import pgtrigger.utils
from django.apps import apps
from django.db import connection

from django_pgconstraints.sql import _col, _walk_aggregate_chain_to_root
from django_pgconstraints.triggers import (
    GeneratedFieldTrigger,
    _build_chain_back_where,
    _GeneratedFieldAggregateReverse,
    _GeneratedFieldReverse,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet


def refresh_dependent(queryset: QuerySet[Any]) -> None:
    """Recompute every GeneratedFieldTrigger target that depends on ``queryset``.

    For each model ``C`` that has a ``GeneratedFieldTrigger`` whose expression
    traverses through ``queryset.model`` — either via a forward FK chain or
    via an aggregate over a reverse relation — issues one targeted
    ``UPDATE <C's table> SET <target> = <target> WHERE ...``. The self-update
    forces ``C``'s BEFORE UPDATE forward trigger to recompute the target
    field from the current expression values.

    No-ops when the queryset matches zero rows or no dependent triggers
    exist on ``queryset.model``. Safe to call from within a transaction.
    """
    root_model = queryset.model
    qn: Callable[[str], str] = pgtrigger.utils.quote

    # Compile the queryset's WHERE clause so we scope the reconciliation
    # to only the rows the caller asked about.
    qs_sql, qs_params = queryset.values("pk").query.sql_with_params()

    with connection.cursor() as cur:
        leaf_sql = cur.mogrify(qs_sql, qs_params)
        if isinstance(leaf_sql, bytes):
            leaf_sql = leaf_sql.decode()

    # Track (owner_model, target_field, related_model) so aggregate triggers
    # — which yield three reverse-trigger entries (INSERT/UPDATE/DELETE) per
    # relation — only produce one self-update per pair.
    handled: set[tuple[str, str, str]] = set()

    for owner_model in apps.get_models():
        for trigger in getattr(owner_model._meta, "triggers", []):  # noqa: SLF001
            if not isinstance(trigger, GeneratedFieldTrigger):
                continue
            for related_model, raw_reverse in trigger.get_reverse_triggers(owner_model):
                if related_model is not root_model:
                    continue
                key = (
                    owner_model._meta.label,  # noqa: SLF001
                    trigger.field,
                    related_model._meta.label,  # noqa: SLF001
                )
                if key in handled:
                    continue
                handled.add(key)
                sql = _build_refresh_sql(
                    owner_model,
                    trigger.field,
                    related_model,
                    raw_reverse,
                    qn,
                    leaf_sql,
                )
                with connection.cursor() as cur:
                    cur.execute(sql)


def _build_refresh_sql(  # noqa: PLR0913
    owner_model: Any,  # noqa: ANN401
    target_field: str,
    related_model: Any,  # noqa: ANN401
    reverse_trigger: pgtrigger.Trigger,
    qn: Callable[[str], str],
    leaf_sql: str,
) -> str:
    """Build the self-update SQL for one reconciliation pass.

    Forward FK reverse: scope the self-update by walking back from the
    queryset's rows to the trigger owner via the chain_back data.

    Aggregate reverse: scope the self-update by finding parent IDs from
    the queryset's filtered children.
    """
    owner_table = qn(owner_model._meta.db_table)  # noqa: SLF001
    target_col = qn(_col(owner_model._meta.get_field(target_field)))  # noqa: SLF001

    if isinstance(reverse_trigger, _GeneratedFieldReverse):
        where = _build_chain_back_where(reverse_trigger.chain_back, qn, leaf_sql)
        return f"UPDATE {owner_table} SET {target_col} = {target_col} WHERE {where}"

    if isinstance(reverse_trigger, _GeneratedFieldAggregateReverse):
        owner_pk = qn(_col(owner_model._meta.pk))  # noqa: SLF001
        leaf_table = qn(related_model._meta.db_table)  # noqa: SLF001
        leaf_pk = qn(_col(related_model._meta.pk))  # noqa: SLF001
        leaf_fk = qn(reverse_trigger.leaf_fk_column)
        # Seed: project the leaf-level FK from rows the queryset matched.
        seed = f"SELECT DISTINCT {leaf_fk} FROM {leaf_table} WHERE {leaf_fk} IS NOT NULL AND {leaf_pk} IN ({leaf_sql})"
        affected = _walk_aggregate_chain_to_root(reverse_trigger.chain, qn, seed)
        return f"UPDATE {owner_table} SET {target_col} = {target_col} WHERE {owner_pk} IN ({affected})"

    msg = f"Unsupported reverse trigger type: {type(reverse_trigger).__name__}"
    raise TypeError(msg)
