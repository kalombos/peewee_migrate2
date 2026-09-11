import re
from collections import namedtuple
from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

import peewee as pw
from playhouse.migrate import Operation

from miggy.deconstructor import ForeignKeyFieldDeconstructor
from miggy.state import State
from miggy.types import ModelCls
from miggy.utils import (
    ModelIndex,
    fk_postfix,
    get_single_index,
    get_single_index_name,
    has_single_index,
    indexes_state,
    resolve_field,
)

if TYPE_CHECKING:
    from miggy.schema import SchemaMigrator

RunPythonF = Callable[["SchemaMigrator", "State"], None]


class Dependency(namedtuple("Dependency", "model_name field_name type")):
    class Type(Enum):
        REMOVE_PK = auto()
        CREATE = auto()


class MigrateOperation:
    """
    Base class for a migrate operation
    """

    def __new__(cls, *args, **kwargs):
        self = object.__new__(cls)
        self._constructor_args = (args, kwargs)
        return self

    @property
    def deps(self) -> list[Dependency]:
        # Workaround to avoid initializing deps in the constructor
        # and calling super() in every MigrateOperation subclass.
        if not hasattr(self, "_deps"):
            self._deps: list[Dependency] = []
        return self._deps

    def get_operation_call(self) -> str:
        name = self.__class__.__name__
        return f"operations.{name}"
        shortcut = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        return f"migrator.{shortcut}"

    def deconstruct(self):
        """
        Return a 3-tuple of class import path (or just name if it lives
        under django.db.migrations), positional arguments, and keyword
        arguments.
        """
        return (
            self.get_operation_call(),
            self._constructor_args[0],
            self._constructor_args[1],
        )

    def state_forwards(self, state: State) -> None:
        """
        Take the state from the previous migration, and mutate it
        so that it matches what this migration would perform.
        """

        raise NotImplementedError

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation] | list[Callable]:
        """
        Perform the mutation on the database schema in the normal
        (forwards) direction.
        The method MUST NOT mutate provided states.
        """
        raise NotImplementedError


class RunPython(MigrateOperation):
    """
    Allows to run custom Python code. **func** should be callable object that accept two arguments;
    the first is an instance of :class:`SchemaMigrator` and the second  is an instance of :class:`State`

    Example::

        def save_user(schema_migrator: SchemaMigrator, current_state: State):
            User = current_state["user"]
            User(
                first_name="First",
                last_name="Last",
            ).save()

        migrator.add_operaion(RunPython(save_user))
    """

    def __init__(self, func: RunPythonF) -> None:
        self.func = func

    def state_forwards(self, state: State) -> None:
        pass

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation] | list[Callable]:
        return [lambda: self.func(schema_migrator, from_state)]


class RunSql(MigrateOperation):
    """
    Allows running of arbitrary SQL on the database -
    useful for more advanced features of database backends that Miggy doesn’t support directly.

    Example::

        migrator.add_operation(
            RunSql(
                'INSERT INTO "user" ("first_name", "last_name") VALUES (%s, %s)',
                (
                    "First",
                    "Last",
                ),
            )
        )
    """

    def __init__(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql = sql
        self.params = params

    def state_forwards(self, state: State) -> None:
        pass

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation] | list[Callable]:
        return [schema_migrator.sql(self.sql, self.params)]


class CreateModel(MigrateOperation):
    """
    Creates a new model in the :class:`State` and a corresponding table in the database to match it.
    """

    def __init__(self, name: str, fields: dict[str, pw.Field], meta: dict[str, Any]) -> None:
        self.name = name
        self.fields = fields
        self.meta = meta

    def state_forwards(self, state: State) -> None:
        state.add_model(self.name, self.fields, self.meta)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Callable]:
        model = to_state[self.name]
        return [schema_migrator.create_table(model)]


class RemoveModel(MigrateOperation):
    """
    Deletes the model from the :class:`State` and its table from the database.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def state_forwards(self, state: State) -> None:
        state.remove_model(self.model_name)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Callable]:
        model = from_state[self.model_name]
        return [schema_migrator.drop_table(model)]


class AddIndex(MigrateOperation):
    """
    Creates an index in the database table for the model with model_name.
    The index will be saved in **Model._meta.indexes_state** dict
    """

    def __init__(
        self,
        model_name: str,
        *fields: str,
        name: str,
        unique: bool = False,
        where: pw.SQL | None = None,
        safe: bool = False,
        concurrently: bool = False,
    ) -> None:
        self.model_name = model_name
        self.fields = fields
        self.unique = unique
        self.where = where
        self.name = name
        self.safe = safe
        self.concurrently = concurrently
        self._index: ModelIndex | None = None

    def build_index(self, model: ModelCls) -> ModelIndex:
        if not self._index:
            self._index = ModelIndex(
                model=model,
                fields=[resolve_field(model, f) for f in self.fields],
                unique=self.unique,
                where=self.where,
                name=self.name,
                safe=self.safe,
                concurrently=self.concurrently,
            )
        return self._index

    def state_forwards(self, state: State) -> None:
        model = state[self.model_name]
        model_index = self.build_index(model)
        indexes_state(model)[model_index._name] = model_index  # type: ignore[attr-defined]

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        model = to_state[self.model_name]
        model_index = self.build_index(model)
        return [schema_migrator.add_model_index(model_index)]


class DropIndex(MigrateOperation):
    """
    Removes the index named name from the model with model_name.
    """

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def state_forwards(self, state: State) -> None:
        model = state[self.model_name]
        del indexes_state(model)[self.name]

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        model = from_state[self.model_name]
        return [schema_migrator.drop_index(model._meta.table_name, self.name)]


class RenameTable(MigrateOperation):
    """
    Renames the model from the old name to a new one.
    It also renames all single-column indexes, if they exist.

    **Warning:**

    This operation does not rename indexes created via the **Meta** class or the **add_index()** method.
    You should explicitly specify index names if you plan to use this operation.
    Otherwise, you will be prompted to recreate the indexes with a new name in the next migration.
    """

    def __init__(self, model_name: str, new_table_name: str) -> None:
        self.model_name = model_name
        self.new_table_name = new_table_name

    def state_forwards(self, state: State) -> None:
        model = state[self.model_name]
        model._meta.table_name = self.new_table_name

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Callable]:
        """Rename table in database."""
        old_model = from_state[self.model_name]
        new_model = to_state[self.model_name]
        ops = [schema_migrator.rename_table(old_model._meta.table_name, self.new_table_name)]
        for old_field in old_model._meta.sorted_fields:
            new_field = getattr(new_model, old_field.name)
            ops.append(schema_migrator.resolve_single_index_name(old_field, new_field))
        return ops


class AddField(MigrateOperation):
    """
    Add a field to a model.
    """

    def __init__(self, model_name: str, name: str, field: pw.Field) -> None:
        self.model_name = model_name
        self.name = name
        self.field = field

    def state_forwards(self, state: State) -> None:
        state.add_field(self.model_name, self.name, self.field)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        model = to_state[self.model_name]
        field = model._meta.fields[self.name]
        return [schema_migrator.add_field(field)]


class AlterField(MigrateOperation):
    """
    Alter a field for a model.
    """

    def __init__(self, model_name: str, name: str, field: pw.Field) -> None:
        self.model_name = model_name
        self.name = name
        self.field = field

    def state_forwards(self, state: State) -> None:
        state.add_field(self.model_name, self.name, self.field)

    def handle_indexes(
        self, old_field: pw.Field, new_field: pw.Field, schema_migrator: "SchemaMigrator"
    ) -> list[Operation]:
        _ops = []
        _field = new_field
        if _field.unique and old_field.unique:
            return []
        if not _field.unique and not old_field.unique and _field.index == old_field.index:
            return []
        table_name = old_field.model._meta.table_name
        if has_single_index(old_field):
            # We have already renamed the column so create name from the new field
            _ops.append(schema_migrator.drop_index(table_name, get_single_index_name(_field)))
        if model_index := get_single_index(_field):
            _ops.append(schema_migrator.add_model_index(model_index))
        return _ops

    def handle_fk_constraint(
        self, old_field: pw.Field, new_field: pw.Field, schema_migrator: "SchemaMigrator"
    ) -> list[Operation]:
        _ops: list[Operation] = []
        is_old_field_fk = isinstance(old_field, pw.ForeignKeyField)
        is_new_field_fk = isinstance(new_field, pw.ForeignKeyField)
        if (
            is_old_field_fk
            and is_new_field_fk
            and (
                ForeignKeyFieldDeconstructor(old_field).deconstruct_fk_params()
                == ForeignKeyFieldDeconstructor(new_field).deconstruct_fk_params()
            )
        ):
            # Nothing's changed for fk
            return _ops
        table_name = old_field.model._meta.table_name
        if is_old_field_fk:
            # we use new_field.column_name because we may have rename column before
            _ops.append(schema_migrator.drop_foreign_key_constraint(table_name, new_field.column_name))
        if is_new_field_fk:
            _ops.append(
                schema_migrator.add_foreign_key_constraint(
                    table_name,
                    new_field.column_name,
                    new_field.rel_model._meta.table_name,  # type: ignore[attr-defined]
                    new_field.rel_field.column_name,  # type: ignore[attr-defined]
                    new_field.on_delete,  # type: ignore[attr-defined]
                    new_field.on_update,  # type: ignore[attr-defined]
                    constraint_name=new_field.constraint_name,  # type: ignore[attr-defined]
                )
            )
        return _ops

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        _ops = []
        name = self.name
        old_model = from_state[self.model_name]
        old_field = getattr(old_model, name)
        old_column_name = old_field.column_name
        table_name = old_model._meta.table_name
        model = to_state[self.model_name]
        field = model._meta.fields[self.name]

        if old_column_name != field.column_name:
            _ops.append(schema_migrator.rename_field(table_name, old_field, field))
        _ops.append(schema_migrator._resolve_alter_column_type(old_field, field))
        _ops.append(schema_migrator._resolve_alter_primary_key(old_field, field))
        _ops.extend(self.handle_fk_constraint(old_field, field, schema_migrator))
        _ops.append(schema_migrator._resolve_alter_default_constraint(old_field, field))
        _ops.append(schema_migrator._resolve_alter_check_constraints(old_field, field))
        if old_field.null != field.null:
            _operation = schema_migrator.drop_not_null if field.null else schema_migrator.add_not_null
            _ops.append(_operation(table_name, field.column_name))
        _ops.extend(self.handle_indexes(old_field, field, schema_migrator))
        return _ops


class RemoveField(MigrateOperation):
    """
    Remove a field from a model
    """

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def state_forwards(self, state: State) -> None:
        state.remove_field(self.model_name, self.name)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:

        model = from_state[self.model_name]
        field = model._meta.fields[self.name]
        return [schema_migrator.drop_column(model._meta.table_name, field.column_name, cascade=False)]


class RenameField(MigrateOperation):
    """
    Changes a field’s name (and, unless **column_name** is set, its column name).
    It also renames a single-column indexe, if it exists.

    **Warning:**

    This operation does not rename indexes created via the **Meta** class or the **add_index()** method.
    You should explicitly specify index names if you plan to use this operation.
    Otherwise, you will be prompted to recreate the index with a new name in the next migration.
    """

    def __init__(self, model_name: str, old_name: str, new_name: str) -> None:
        self.model_name = model_name
        self.old_field_name = old_name
        self.new_field_name = new_name

    def state_forwards(self, state: State) -> None:
        model = state[self.model_name]

        old_field = model._meta.fields[self.old_field_name]
        new_field = old_field.clone()
        new_field.column_name = self.resolve_new_name(old_field, self.new_field_name)

        state.remove_field(self.model_name, self.old_field_name)
        state.add_field(self.model_name, self.new_field_name, new_field)

    def resolve_new_name(self, old_field: pw.Field, new_name: str) -> str:
        if isinstance(old_field, pw.ForeignKeyField):
            if old_field.column_name == fk_postfix(old_field.name):
                return fk_postfix(new_name)
        if old_field.column_name == old_field.name:
            return new_name
        return old_field.column_name

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        old_model = from_state[self.model_name]
        new_model = to_state[self.model_name]
        old_field = old_model._meta.fields[self.old_field_name]
        new_field = new_model._meta.fields[self.new_field_name]
        if old_field.column_name != new_field.column_name:
            return [schema_migrator.rename_field(new_model._meta.table_name, old_field, new_field)]
        return []


class ChangeNullable(MigrateOperation):
    def __init__(self, model_name: str, *names: str, is_null: bool) -> None:
        self.model_name = model_name
        self.names = names
        self.is_null = is_null

    def state_forwards(self, state: State) -> None:
        model = state[self.model_name]
        for name in self.names:
            field = model._meta.fields[name]
            field.null = self.is_null

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        ops = []
        model = to_state[self.model_name]
        for name in self.names:
            field = model._meta.fields[name]
            _operation = schema_migrator.drop_not_null if self.is_null else schema_migrator.add_not_null
            ops.append(_operation(model._meta.table_name, field.column_name))
        return ops


class AddCheckConstraint(MigrateOperation):
    """Add a check constraint."""

    def __init__(self, model_name: str, name: str, constraint: str) -> None:
        self.model_name = model_name
        self.name = name
        self.constraint = constraint

    def state_forwards(self, state: State) -> None:
        state.add_check_constraint(self.model_name, self.name, self.constraint)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        table_name = to_state[self.model_name]._meta.table_name
        return [schema_migrator.add_check_constraint(table_name, self.name, self.constraint)]


class RemoveCheckConstraint(MigrateOperation):
    """Remove a check constraint."""

    def __init__(self, model_name: str, name: str) -> None:
        self.model_name = model_name
        self.name = name

    def state_forwards(self, state: State) -> None:
        state.remove_check_constraint(self.model_name, self.name)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        table_name = to_state[self.model_name]._meta.table_name
        return [schema_migrator.drop_constraint(table_name, self.name)]


class AddPrimaryKeyConstraint(MigrateOperation):
    """Add a primary key constraint."""

    def __init__(self, model_name: str, *fields: str) -> None:
        self.model_name = model_name
        self.fields = fields

    def state_forwards(self, state: State) -> None:
        state.add_composite_key(self.model_name, pw.CompositeKey(*self.fields))

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        model = to_state[self.model_name]
        columns = [model._meta.fields[f].column_name for f in self.fields]
        return [schema_migrator.add_primary_key_constraint(model._meta.table_name, *columns)]


class RemovePrimaryKeyConstraint(MigrateOperation):
    """Remove a primary key constraint."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def state_forwards(self, state: State) -> None:
        state.remove_composite_key(self.model_name)

    def database_forwards(
        self, schema_migrator: "SchemaMigrator", from_state: State, to_state: State
    ) -> list[Operation]:
        model = from_state[self.model_name]
        return [schema_migrator.drop_primary_key_constraint(model._meta.table_name)]
