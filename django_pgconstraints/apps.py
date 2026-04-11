"""Django app configuration for django-pgconstraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.core.checks import Error, Tags, register

if TYPE_CHECKING:
    from django.core.checks import CheckMessage


class PgConstraintsConfig(AppConfig):
    name = "django_pgconstraints"
    default_auto_field = "django.db.models.BigAutoField"


@register(Tags.models)
def check_triggers_not_in_constraints(**kwargs: Any) -> list[CheckMessage]:  # noqa: ANN401, ARG001
    """Raise an error if a pgconstraints trigger is placed in Meta.constraints."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import (  # noqa: PLC0415
        AllowedTransitions,
        CheckConstraintTrigger,
        Immutable,
        UniqueConstraintTrigger,
        _MaintainedCountBase,
    )

    trigger_types = (
        UniqueConstraintTrigger,
        CheckConstraintTrigger,
        AllowedTransitions,
        Immutable,
        _MaintainedCountBase,
    )

    return [
        Error(
            f"{constraint.__class__.__name__} belongs in Meta.triggers, not Meta.constraints.",
            hint="See django-pgtrigger docs for Meta.triggers usage.",
            obj=model,
            id="pgconstraints.E001",
        )
        for model in apps.get_models()
        for constraint in model._meta.constraints  # noqa: SLF001
        if isinstance(constraint, trigger_types)
    ]
