"""Public refresh_dependent helper.

Walks the GeneratedFieldTrigger registry at call time and issues one
targeted recompute UPDATE per dependent child model / target-field pair.
Used to reconcile computed fields after a bulk load or raw-SQL operation
that bypassed the BEFORE INSERT/UPDATE forward trigger.
"""

from typing import TYPE_CHECKING, Any, cast

import pgtrigger
import pgtrigger.utils
from django.apps import apps
from django.db import connection

from django_pgconstraints.sql import _col
from django_pgconstraints.triggers import (
    GeneratedFieldTrigger,
    _build_chain_back_where,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet

    from django_pgconstraints.triggers import _GeneratedFieldReverse


def refresh_dependent(queryset: QuerySet[Any]) -> None:
    """Recompute every GeneratedFieldTrigger target that depends on ``queryset``.

    For each model ``C`` that has a ``GeneratedFieldTrigger`` whose expression
    traverses through ``queryset.model``, issues one targeted
    ``UPDATE <C's table> SET <target> = <target> WHERE <chain_back
    resolves to a row in queryset>``. The self-update forces ``C``'s
    BEFORE UPDATE forward trigger to recompute the target field from
    the current expression values.

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

    # Walk every GeneratedFieldTrigger in the app and find ones whose
    # reverse triggers would fire for `root_model`.
    for child_model in apps.get_models():
        for trigger in getattr(child_model._meta, "triggers", []):  # noqa: SLF001
            if not isinstance(trigger, GeneratedFieldTrigger):
                continue
            for related_model, raw_reverse in trigger.get_reverse_triggers(child_model):
                if related_model is not root_model:
                    continue
                reverse_trigger = cast("_GeneratedFieldReverse", raw_reverse)
                child_table = qn(child_model._meta.db_table)  # noqa: SLF001
                target_col = qn(_col(child_model._meta.get_field(trigger.field)))  # noqa: SLF001
                where = _build_chain_back_where(reverse_trigger.chain_back, qn, leaf_sql)
                sql = f"UPDATE {child_table} SET {target_col} = {target_col} WHERE {where}"
                with connection.cursor() as cur:
                    cur.execute(sql)
