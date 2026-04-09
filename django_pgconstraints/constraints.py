"""Cross-table constraint implementations backed by PostgreSQL triggers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS
from django.db.models import BaseConstraint

if TYPE_CHECKING:
    from django.apps import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.models import Model


class CrossTableUnique(BaseConstraint):
    """Enforce uniqueness of a field's value across two tables.

    Uses a deferrable constraint trigger that checks the other table
    on INSERT or UPDATE and raises a unique-violation error (SQLSTATE 23505)
    if a duplicate is found.

    Each table in the pair needs its own ``CrossTableUnique`` constraint
    pointing at the other table.  Within-table uniqueness is **not** enforced
    by this constraint — use Django's ``UniqueConstraint`` for that.
    """

    violation_error_code = "cross_table_unique"
    violation_error_message = "This value already exists in a related table."

    def __init__(  # noqa: PLR0913
        self,
        *,
        field: str,
        across: str,
        across_field: str | None = None,
        name: str,
        violation_error_code: str | None = None,
        violation_error_message: str | None = None,
    ) -> None:
        self.field = field
        self.across = across
        self.across_field = across_field or field
        super().__init__(
            name=name,
            violation_error_code=violation_error_code,
            violation_error_message=violation_error_message,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_across_model(self, apps: Apps) -> type[Model]:
        app_label, model_name = self.across.split(".")
        return apps.get_model(app_label, model_name)

    def _function_name(self) -> str:
        return f"pgc_fn_{self.name}"

    # ------------------------------------------------------------------
    # Schema SQL
    # ------------------------------------------------------------------

    def constraint_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> None:
        # Triggers cannot live inside a CREATE TABLE statement, but
        # table_sql() only calls create_sql() when parameters are present.
        # Append our trigger DDL to deferred_sql so it runs after CREATE TABLE
        # regardless of the code path.
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))

    def create_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        column = model._meta.get_field(self.field).column  # noqa: SLF001

        across_model = self._get_across_model(model._meta.apps)  # noqa: SLF001
        across_table = across_model._meta.db_table  # noqa: SLF001
        across_column = across_model._meta.get_field(self.across_field).column  # noqa: SLF001

        fn_name = self._function_name()

        function_sql = (
            f"CREATE FUNCTION {qn(fn_name)}() RETURNS TRIGGER AS $body$ "
            f"BEGIN "
            f"IF NEW.{qn(column)} IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM {qn(across_table)} "
            f"WHERE {qn(across_column)} = NEW.{qn(column)} "
            f"FOR UPDATE"
            f") THEN "
            f"RAISE EXCEPTION "
            f"'Cross-table unique constraint \"%%s\" is violated.', '{self.name}' "
            f"USING ERRCODE = '23505', "
            f"CONSTRAINT = '{self.name}'; "
            f"END IF; "
            f"RETURN NEW; "
            f"END; "
            f"$body$ LANGUAGE plpgsql"
        )
        schema_editor.execute(function_sql)

        return (
            f"CREATE CONSTRAINT TRIGGER {qn(self.name)} "
            f"AFTER INSERT OR UPDATE OF {qn(column)} ON {qn(table)} "
            f"DEFERRABLE INITIALLY DEFERRED "
            f"FOR EACH ROW "
            f"EXECUTE FUNCTION {qn(fn_name)}()"
        )

    def remove_sql(self, model: type[Model], schema_editor: BaseDatabaseSchemaEditor) -> str:
        qn = schema_editor.quote_name
        table = model._meta.db_table  # noqa: SLF001
        fn_name = self._function_name()

        schema_editor.execute(f"DROP TRIGGER IF EXISTS {qn(self.name)} ON {qn(table)}")
        return f"DROP FUNCTION IF EXISTS {qn(fn_name)}()"

    # ------------------------------------------------------------------
    # Python-level validation
    # ------------------------------------------------------------------

    def validate(
        self,
        model: type[Model],  # noqa: ARG002
        instance: Model,
        exclude: set[str] | None = None,
        using: str = DEFAULT_DB_ALIAS,
    ) -> None:
        if exclude and self.field in exclude:
            return

        value = getattr(instance, self.field)
        if value is None:
            return

        across_model = self._get_across_model(instance._meta.apps)  # noqa: SLF001
        if across_model.objects.using(using).filter(**{self.across_field: value}).exists():
            raise ValidationError(self.get_violation_error_message(), code=self.violation_error_code)

    # ------------------------------------------------------------------
    # Migration serialisation
    # ------------------------------------------------------------------

    def deconstruct(self) -> tuple[str, tuple[()], dict[str, str]]:
        path, args, kwargs = super().deconstruct()
        path = path.replace("django_pgconstraints.constraints", "django_pgconstraints")
        kwargs["field"] = self.field
        kwargs["across"] = self.across
        if self.across_field != self.field:
            kwargs["across_field"] = self.across_field
        return path, args, kwargs

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CrossTableUnique):
            return (
                self.name == other.name
                and self.field == other.field
                and self.across == other.across
                and self.across_field == other.across_field
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.name, self.field, self.across, self.across_field))

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: field={self.field!r} across={self.across!r}>"
