"""Cycle detection for GeneratedFieldTrigger dependencies.

Runs once at ``AppConfig.ready()`` time to catch cyclic dependencies that
would cause runaway trigger recursion at runtime.  No persistent state —
the adjacency map is built transiently, walked with DFS, and discarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model
    from django.db.models.expressions import BaseExpression

    from django_pgconstraints.triggers import GeneratedFieldTrigger


_NodeKey = tuple[str, str]  # (model_label, field_name)


class CycleError(Exception):
    """Raised when a GeneratedFieldTrigger dependency graph contains a cycle.

    The *path* attribute is a list of ``"<model_label>.<field>"`` strings
    that form the cycle, starting and ending with the same entry so the
    error message renders as a closed loop.
    """

    def __init__(self, path: list[str]) -> None:
        self.path = path
        rendered = " → ".join(path)
        super().__init__(f"Computed field cycle detected: {rendered}")


def check_for_cycles(
    specs: list[tuple[type[Model], GeneratedFieldTrigger]],
) -> None:
    """Walk every trigger's expression and raise :class:`CycleError` on cycles.

    *specs* is a list of ``(owning_model, trigger)`` pairs — typically what
    you get by iterating ``apps.get_models()`` and filtering
    ``model._meta.triggers`` for ``GeneratedFieldTrigger`` instances.
    """
    edges: dict[_NodeKey, set[_NodeKey]] = {}
    nodes: set[_NodeKey] = set()

    for model, trigger in specs:
        target: _NodeKey = (model._meta.label, trigger.field)  # noqa: SLF001
        nodes.add(target)
        for chain in _iter_f_refs(trigger.expression):
            _add_chain_edges(edges, nodes, model, chain, target)

    _detect(edges, nodes)


def _iter_f_refs(expr: BaseExpression) -> Iterator[str]:
    """Yield every ``F()`` reference name in *expr* (local and FK-traversed)."""
    from django.db.models import F  # noqa: PLC0415

    if isinstance(expr, F):
        yield expr.name
        return
    for child in expr.get_source_expressions():
        if child is not None:
            yield from _iter_f_refs(child)


def _add_chain_edges(
    edges: dict[_NodeKey, set[_NodeKey]],
    nodes: set[_NodeKey],
    model: type[Model],
    chain: str,
    target: _NodeKey,
) -> None:
    """Add edges for one ``F()`` chain (local or FK-traversed) into the map.

    A local reference ``F("price")`` adds one edge
    ``(model, price) → target``.

    A traversed reference ``F("part__supplier__markup_pct")`` adds one edge
    per hop along the chain plus the leaf edge, so that *both* reassigning
    an intermediate FK and changing the leaf value produce entries in the
    graph.  Concretely the chain produces:

    - ``(PurchaseItem, part) → target``      (FK reassignment on child)
    - ``(Part, supplier) → target``          (FK reassignment on intermediate)
    - ``(Supplier, markup_pct) → target``    (leaf value change)

    This mirrors how :meth:`GeneratedFieldTrigger.get_reverse_triggers`
    installs one reverse trigger per source — each FK hop is a valid cycle
    entry point because reassigning it cascades a recompute.
    """
    parts = chain.split("__")
    current_model = model
    for i, part in enumerate(parts):
        field = current_model._meta.get_field(part)  # noqa: SLF001
        source: _NodeKey = (current_model._meta.label, part)  # noqa: SLF001
        nodes.add(source)
        nodes.add(target)
        edges.setdefault(source, set()).add(target)
        if not field.is_relation:
            if i != len(parts) - 1:
                msg = f"Non-relation field {part!r} in middle of chain {chain!r}"
                raise ValueError(msg)
            return
        current_model = field.related_model  # type: ignore[assignment]


def _detect(
    edges: dict[_NodeKey, set[_NodeKey]],
    nodes: set[_NodeKey],
) -> None:
    """Recursive DFS cycle check.  Raises :class:`CycleError` on first cycle."""
    visited: set[_NodeKey] = set()
    stack: list[_NodeKey] = []
    on_stack: set[_NodeKey] = set()

    def visit(node: _NodeKey) -> None:
        if node in on_stack:
            start = stack.index(node)
            cycle = [*stack[start:], node]
            raise CycleError([f"{m}.{f}" for m, f in cycle])
        if node in visited:
            return
        stack.append(node)
        on_stack.add(node)
        for dep in edges.get(node, set()):
            visit(dep)
        stack.pop()
        on_stack.remove(node)
        visited.add(node)

    for node in list(nodes):
        if node not in visited:
            visit(node)
