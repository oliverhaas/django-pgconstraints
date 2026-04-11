"""Shared trigger lifecycle helpers for tests.

- `trigger_exists(name_fragment, table)` queries pg_trigger to check whether a
  trigger with a name containing `name_fragment` is attached to `table`.
- `swap_trigger(model, new_trigger, *, index=0)` is a context manager that
  temporarily uninstalls the model's Nth `Meta.triggers` entry, installs
  `new_trigger`, and restores the original on exit.  This is what deferred /
  dynamic-condition tests use instead of copy-pasted try/finally blocks.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model

    from django_pgconstraints.triggers import (
        CheckConstraintTrigger,
        GeneratedFieldTrigger,
        UniqueConstraintTrigger,
    )

    Trigger = UniqueConstraintTrigger | CheckConstraintTrigger | GeneratedFieldTrigger


def trigger_exists(name_fragment: str, table: str) -> bool:
    """Return True if a trigger whose name contains `name_fragment` exists on `table`."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname LIKE %s AND c.relname = %s",
            [f"%{name_fragment}%", table],
        )
        return cur.fetchone() is not None


@contextmanager
def swap_trigger(
    model: type[Model],
    new_trigger: Trigger,
    *,
    index: int = 0,
) -> Iterator[Trigger]:
    """Temporarily replace `model._meta.triggers[index]` with `new_trigger`.

    Uninstalls the original, installs the replacement, yields it, then
    restores the original on exit (even on exception).
    """
    original = model._meta.triggers[index]  # type: ignore[attr-defined]
    original.uninstall(model)
    try:
        new_trigger.install(model)
        yield new_trigger
    finally:
        new_trigger.uninstall(model)
        original.install(model)
