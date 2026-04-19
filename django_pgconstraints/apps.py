"""Django app configuration for django-pgconstraints."""

from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.core.checks import Error, Tags, register
from django.db import router
from django.db.models.signals import post_migrate

if TYPE_CHECKING:
    from django.core.checks import CheckMessage


class PgConstraintsConfig(AppConfig):
    name = "django_pgconstraints"

    def ready(self) -> None:
        _check_and_register_reverse_triggers()
        _register_auto_refresh()
        # Connect without a sender so we install indexes for models in any
        # app (e.g. testapp) whenever post_migrate fires. dispatch_uid keeps
        # the receiver idempotent if ready() runs twice (some test runners
        # and management-command paths trigger this).
        post_migrate.connect(
            _install_unique_indexes,
            dispatch_uid="django_pgconstraints.install_unique_indexes",
        )


def _install_unique_indexes(
    sender: Any = None,  # noqa: ANN401
    using: str | None = None,
    **_kwargs: Any,  # noqa: ANN401
) -> None:
    """Install CREATE UNIQUE INDEX for every UniqueConstraintTrigger(index=True).

    pgtrigger's schema-editor patch installs the PL/pgSQL trigger when it
    creates a model's table, but it bypasses our `install()` override. We
    install the backing index ourselves from a post_migrate signal so that
    both `migrate` and the pytest-django `--create-db` path end up with the
    index in place.

    The signal is connected without a sender filter and fires once per app
    during migrate; we scope the per-app work by filtering the app's own
    models so we only touch each trigger once per migrate pass.
    """
    from django_pgconstraints.triggers import UniqueConstraintTrigger  # noqa: PLC0415

    if sender is None:
        return
    # TODO(follow-up): drift detection. If the user changes the trigger's  # noqa: FIX002, TD003
    # indexable configuration (fields, expression, condition), the existing
    # index stays under the same name because CREATE INDEX IF NOT EXISTS is
    # a no-op. A future pass should DROP + recreate when the computed index
    # definition differs from the existing one, or hash the definition into
    # the index name so a config change produces a new name.
    for model in sender.get_models():
        if using is not None and not router.allow_migrate_model(using, model):
            continue
        for trigger in getattr(model._meta, "triggers", []):  # noqa: SLF001
            if isinstance(trigger, UniqueConstraintTrigger) and trigger.index:
                trigger._install_index(model, database=using)  # noqa: SLF001


def _register_auto_refresh() -> None:
    """Wire RETURNING-based refresh for every ``GeneratedFieldTrigger``."""
    from django_pgconstraints.returning import register_auto_refresh  # noqa: PLC0415

    register_auto_refresh()


def _check_and_register_reverse_triggers() -> None:
    """Scan every model for GeneratedFieldTrigger instances, run the cycle
    check, then register reverse triggers on related models."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.cycles import check_for_cycles  # noqa: PLC0415
    from django_pgconstraints.triggers import GeneratedFieldTrigger  # noqa: PLC0415

    specs: list[tuple[type, GeneratedFieldTrigger]] = [
        (model, trigger)
        for model in apps.get_models()
        for trigger in getattr(model._meta, "triggers", [])  # noqa: SLF001
        if isinstance(trigger, GeneratedFieldTrigger)
    ]

    check_for_cycles(specs)

    for model, trigger in specs:
        for related_model, reverse_trigger in trigger.get_reverse_triggers(model):
            reverse_trigger.register(related_model)  # type: ignore[arg-type]


@register(Tags.models)
def check_triggers_not_in_constraints(**kwargs: Any) -> list[CheckMessage]:  # noqa: ANN401, ARG001
    """Raise an error if a pgconstraints trigger is placed in Meta.constraints."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import (  # noqa: PLC0415
        CheckConstraintTrigger,
        GeneratedFieldTrigger,
        UniqueConstraintTrigger,
    )

    trigger_types = (
        UniqueConstraintTrigger,
        CheckConstraintTrigger,
        GeneratedFieldTrigger,
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
