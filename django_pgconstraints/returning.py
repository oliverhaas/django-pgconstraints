"""Piggyback RETURNING onto save()/bulk_create() for GeneratedFieldTrigger targets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.db.models import Field, Model


# One dynamic subclass per original field class, shared across every field we
# patch of that class. Keeps isinstance() checks stable.
_PATCHED_FIELD_CLASSES: dict[type, type] = {}


def register_auto_refresh() -> None:
    """Wire RETURNING-based refresh for every ``GeneratedFieldTrigger(auto_refresh=True)``."""
    from django.apps import apps  # noqa: PLC0415

    from django_pgconstraints.triggers import GeneratedFieldTrigger  # noqa: PLC0415

    model_fields: dict[type[Model], list[str]] = {}
    for model in apps.get_models():
        for trigger in getattr(model._meta, "triggers", []):  # noqa: SLF001
            if isinstance(trigger, GeneratedFieldTrigger) and trigger.auto_refresh:
                model_fields.setdefault(model, []).append(trigger.field)

    for model, field_names in model_fields.items():
        fields: list[Field] = [
            model._meta.get_field(name)  # type: ignore[misc]  # noqa: SLF001
            for name in field_names
        ]
        for field in fields:
            _patch_field_db_returning(field)
        # Bust the cached property so the newly-flagged fields are picked up.
        model._meta.__dict__.pop("db_returning_fields", None)  # noqa: SLF001
        _install_do_update_override(model, fields)


def _patch_field_db_returning(field: Field) -> None:
    # Field.db_returning is a property with no setter, so we can't assign on
    # the instance. Swap in a subclass that declares db_returning = True — MRO
    # lookup finds the class attribute before the inherited property.
    cls = type(field)
    if cls.__dict__.get("db_returning") is True:
        return
    if cls not in _PATCHED_FIELD_CLASSES:
        _PATCHED_FIELD_CLASSES[cls] = type(
            f"_AutoReturning{cls.__name__}",
            (cls,),
            {"db_returning": True},
        )
    field.__class__ = _PATCHED_FIELD_CLASSES[cls]


def _install_do_update_override(model: type[Model], fields: list[Field]) -> None:
    # Model._save_table passes its returning_fields list to _do_update and
    # then reuses the same list for _assign_returned_values. Mutating it in
    # place here threads our columns through both the UPDATE RETURNING SQL
    # and the post-update assignment on self.
    if getattr(model, "_pgc_auto_refresh_patched", False):
        tracked: list[Field] = model._pgc_auto_refresh_fields  # type: ignore[attr-defined]  # noqa: SLF001
        for field in fields:
            if field not in tracked:
                tracked.append(field)
        return

    model._pgc_auto_refresh_fields = list(fields)  # type: ignore[attr-defined]  # noqa: SLF001
    original = model._do_update  # noqa: SLF001

    def _do_update(  # noqa: PLR0913
        self: Model,
        base_qs: Any,  # noqa: ANN401
        using: str,
        pk_val: Any,  # noqa: ANN401
        values: Any,  # noqa: ANN401
        update_fields: Any,  # noqa: ANN401
        forced_update: bool,  # noqa: FBT001
        returning_fields: list[Field],
    ) -> Any:  # noqa: ANN401
        for field in type(self)._pgc_auto_refresh_fields:  # type: ignore[attr-defined]  # noqa: SLF001
            if field not in returning_fields:
                returning_fields.append(field)
        return original(
            self,
            base_qs,
            using,
            pk_val,
            values,
            update_fields,
            forced_update,
            returning_fields,
        )

    model._do_update = _do_update  # type: ignore[assignment,method-assign]  # noqa: SLF001
    model._pgc_auto_refresh_patched = True  # type: ignore[attr-defined]  # noqa: SLF001
