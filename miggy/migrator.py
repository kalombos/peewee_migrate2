from typing import TYPE_CHECKING, Any

import peewee as pw
from playhouse.migrate import (
    Operation,
)

from miggy import LOGGER
from miggy.deconstructor import ModelDeconstructor
from miggy.operations import (
    AddCheckConstraint,
    AddField,
    AddIndex,
    AddPrimaryKeyConstraint,
    AlterField,
    ChangeNullable,
    CreateModel,
    DropIndex,
    MigrateOperation,
    RemoveCheckConstraint,
    RemoveField,
    RemoveModel,
    RemovePrimaryKeyConstraint,
    RenameField,
    RenameTable,
    RunPython,
    RunPythonF,
    RunSql,
)
from miggy.schema import SchemaMigrator
from miggy.state import State
from miggy.types import ModelCls

if TYPE_CHECKING:
    from collections.abc import Callable


class Migrator(object):
    """
    A class that provides shortcuts for adding migration operations.
    """

    def __init__(self, database, schema=None):
        """Initialize the migrator."""
        if isinstance(database, pw.Proxy):
            database = database.obj

        self.database = database
        self.state = State()
        self.schema_migrator = SchemaMigrator.from_database(self.database)
        self.schema = schema
        self._operations: list[Operation | Callable] = []

    def add_operation(self, op: MigrateOperation) -> None:
        """
        Adds a migrate operation
        """
        self.state.create_snapshot()
        op.state_forwards(self.state)
        from_state = self.state.pop_snapshot()
        self._operations.extend(op.database_forwards(self.schema_migrator, from_state, self.state))

    def _apply_operations(self) -> None:

        if self.schema:
            _ops = [self.schema_migrator.select_schema(self.schema), *self._operations]
        else:
            _ops = [*self._operations]

        for op in _ops:
            if isinstance(op, Operation):
                LOGGER.info("%s %s", op.method, op.args)
                op.run()
            else:
                op()

    def run(self, change_schema: bool = True):
        if change_schema:
            self._apply_operations()
        self._operations = []

    def python(self, func: RunPythonF):
        """A shortcut for adding a :class:`RunPython` operation."""
        self.add_operation(RunPython(func))

    def sql(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        """A shortcut for adding a :class:`RunSql` operation."""
        self.add_operation(RunSql(sql, params))

    def create_model(
        self,
        name: ModelCls | str,
        fields: dict[str, pw.Field] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ModelCls | None:
        """A shortcut for adding a :class:`CreateModel` operation."""
        if isinstance(name, str):
            fields = fields or {}
            meta = meta or {}
            self.add_operation(CreateModel(name, fields, meta))
            return None
        else:
            # Legacy API
            deconstructed = ModelDeconstructor(name).deconstruct()
            self.add_operation(CreateModel(**deconstructed))
            return name

    create_table = create_model

    def remove_model(self, model_name: str) -> None:
        """A shortcut for adding a :class:`RemoveModel` operation."""

        self.add_operation(RemoveModel(model_name))

    drop_table = remove_model

    def add_field(self, model_name: str, name: str, field: pw.Field) -> None:
        """A shortcut for adding a :class:`AddField` operation."""

        self.add_operation(AddField(model_name, name, field))

    def add_fields(self, model_name: str, **fields: Any) -> None:

        for name, field in fields.items():
            self.add_operation(AddField(model_name, name, field))

    add_columns = add_fields

    def alter_field(self, model_name: str, name: str, field: pw.Field) -> None:
        """A shortcut for adding a :class:`AlterField` operation."""

        self.add_operation(AlterField(model_name, name, field))

    def change_fields(self, model_name: str, **fields: pw.Field) -> None:
        """A shortcut for adding a :class:`ChangeFields` operation."""

        for name, field in fields.items():
            self.add_operation(AlterField(model_name, name, field))

    change_columns = change_fields

    def remove_field(self, model_name: str, name: str) -> None:
        """A shortcut for adding a :class:`RemoveField` operation."""

        self.add_operation(RemoveField(model_name, name))

    def remove_fields(self, model_name: str, *names: str) -> None:
        for name in names:
            self.add_operation(RemoveField(model_name, name))

    drop_columns = remove_fields

    def rename_field(self, model_name: str, old_name: str, new_name: str) -> None:
        """A shortcut for adding a :class:`RenameField` operation."""

        self.add_operation(RenameField(model_name, old_name, new_name))

    rename_column = rename_field

    def rename_table(self, model_name: str, new_table_name: str) -> None:
        """A shortcut for adding a :class:`RenameTable` operation."""

        self.add_operation(RenameTable(model_name, new_table_name))

    rename_model = rename_table

    def add_index(
        self,
        model_name: str,
        *fields: str,
        name: str,
        unique: bool = False,
        where: pw.SQL | None = None,
        safe: bool = False,
        concurrently: bool = False,
    ) -> None:
        """A shortcut for adding a :class:`AddIndex` operation."""

        self.add_operation(
            AddIndex(model_name, *fields, name=name, unique=unique, where=where, safe=safe, concurrently=concurrently)
        )

    def drop_index(self, model_name: str, name: str) -> None:
        """A shortcut for adding a :class:`DropIndex` operation."""

        self.add_operation(DropIndex(model_name, name))

    def add_not_null(self, model_name: str, *names: str) -> None:
        self.add_operation(ChangeNullable(model_name, *names, is_null=False))

    def drop_not_null(self, model_name: str, *names: str) -> None:
        """Drop not null."""
        self.add_operation(ChangeNullable(model_name, *names, is_null=True))

    def add_primary_key_constraint(self, model_name: str, *fields: str) -> None:
        """A shortcut for adding a :class:`AddPrimaryKeyConstraint` operation."""
        self.add_operation(AddPrimaryKeyConstraint(model_name, *fields))

    def remove_primary_key_constraint(self, model_name: str) -> None:
        """A shortcut for adding a :class:`RemovePrimaryKeyConstraint` operation."""
        self.add_operation(RemovePrimaryKeyConstraint(model_name))

    def add_check_constraint(self, model_name: str, name: str, constraint: str) -> None:
        """A shortcut for adding a :class:`AddCheckConstraint` operation."""
        self.add_operation(AddCheckConstraint(model_name, name, constraint))

    def remove_check_constraint(self, model_name: str, name: str) -> None:
        """A shortcut for adding a :class:`RemoveCheckConstraint` operation."""
        self.add_operation(RemoveCheckConstraint(model_name, name))
