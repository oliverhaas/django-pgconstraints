"""Standalone validation helpers mirroring the constraint triggers.

These functions can be used in form/serializer validation without
requiring a constraint instance.  They mirror the Python-side
``validate()`` logic of :class:`~django_pgconstraints.UniqueConstraintTrigger`,
:class:`~django_pgconstraints.AllowedTransitions`, and
:class:`~django_pgconstraints.Immutable`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model, Q


def validate_unique_across(  # noqa: PLR0913
    *,
    instance: Model,
    field: str,
    across: str,
    across_field: str | None = None,
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This value already exists in the referenced table.",
    error_code: str = "cross_table_unique",
) -> None:
    """Raise ``ValidationError`` if *field* on *instance* duplicates a value in *across*.

    Parameters
    ----------
    instance:
        The model instance being validated.
    field:
        The field name on *instance* whose value must be unique.
    across:
        An ``"app_label.ModelName"`` string identifying the model to
        check against (resolved lazily via ``apps.get_model``).
    across_field:
        The field name on the *across* model.  Defaults to *field*.
    using:
        Database alias.
    error_message:
        Human-readable message for the ``ValidationError``.
    error_code:
        Machine-readable code for the ``ValidationError``.
    """
    value = getattr(instance, field)
    if value is None:
        return

    if across_field is None:
        across_field = field

    across_model: type[Model] = apps.get_model(across)
    if across_model.objects.using(using).filter(**{across_field: value}).exists():  # type: ignore[attr-defined]
        raise ValidationError(error_message, code=error_code)


def validate_allowed_transition(  # noqa: PLR0913
    *,
    instance: Model,
    field: str,
    transitions: dict[Any, Sequence[Any]],
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This transition is not allowed.",
    error_code: str = "invalid_transition",
) -> None:
    """Raise ``ValidationError`` if the field change is not in *transitions*.

    Parameters
    ----------
    instance:
        The model instance being validated.
    field:
        The field name whose transition is checked.
    transitions:
        Mapping of ``{old_value: [allowed_new_values, ...]}``.
    using:
        Database alias.
    error_message:
        Human-readable message for the ``ValidationError``.
    error_code:
        Machine-readable code for the ``ValidationError``.
    """
    if instance.pk is None:
        return  # new instance — no old value to transition from

    model = type(instance)
    new_value = getattr(instance, field)

    try:
        old_value = (
            model._default_manager.using(using)  # noqa: SLF001
            .values_list(field, flat=True)
            .get(pk=instance.pk)
        )
    except model.DoesNotExist:  # type: ignore[attr-defined]
        return

    if old_value == new_value:
        return

    # Look up allowed transitions using actual values, NOT str() coercion.
    allowed = transitions.get(old_value, [])
    if new_value not in allowed:
        raise ValidationError(error_message, code=error_code)


def validate_immutable(  # noqa: PLR0913
    *,
    instance: Model,
    fields: list[str],
    when: Q | None = None,
    using: str = DEFAULT_DB_ALIAS,
    error_message: str = "This field cannot be changed.",
    error_code: str = "immutable_field",
) -> None:
    """Raise ``ValidationError`` if any of *fields* have been modified.

    Parameters
    ----------
    instance:
        The model instance being validated.
    fields:
        Field names that must remain unchanged.
    when:
        Optional ``Q`` object.  If provided, fields are only immutable
        while the **old** row in the database matches the condition.
    using:
        Database alias.
    error_message:
        Human-readable message for the ``ValidationError``.
    error_code:
        Machine-readable code for the ``ValidationError``.
    """
    if instance.pk is None:
        return

    if not fields:
        return

    model = type(instance)

    qs = model._default_manager.using(using).filter(pk=instance.pk)  # noqa: SLF001
    if when is not None:
        qs = qs.filter(when)

    try:
        old_values = qs.values(*fields).get()
    except model.DoesNotExist:  # type: ignore[attr-defined]
        return  # row doesn't exist or condition not met

    for field_name in fields:
        if old_values[field_name] != getattr(instance, field_name):
            raise ValidationError(error_message, code=error_code)
