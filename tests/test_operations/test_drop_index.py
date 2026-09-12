import peewee as pw
import pytest

from miggy.operations import DropIndex
from miggy.schema import SchemaMigrator
from miggy.state import State
from miggy.utils import ModelIndex, indexes_state
from tests.conftest import PatchedPgDatabase


def test_state_forwards() -> None:
    class User(pw.Model):
        name = pw.CharField()

    state = State({"user": User})
    indexes_state(state["user"])["some_name"] = ModelIndex(state["user"], (state["user"].name,), name="some_name")

    operation = DropIndex("user", "some_name")
    operation.state_forwards(state)

    assert indexes_state(state["user"]) == {}


def test_state_forwards_missing_index_raises() -> None:
    class User(pw.Model):
        name = pw.CharField()

    state = State({"user": User})
    operation = DropIndex("user", "some_name")

    with pytest.raises(KeyError):
        operation.state_forwards(state)


def test_database_forwards(patched_pg_db: PatchedPgDatabase) -> None:
    class User(pw.Model):
        name = pw.CharField()

        class Meta:
            database = patched_pg_db

    User.create_table()
    patched_pg_db.clear_queries()

    schema_migrator = SchemaMigrator.from_database(patched_pg_db)
    schema_migrator.add_model_index(ModelIndex(User, (User.name,), name="some_name")).run()
    patched_pg_db.clear_queries()

    from_state = State({"user": User})
    operation = DropIndex("user", "some_name")

    operation.database_forwards(schema_migrator, from_state, State())[0].run()

    assert patched_pg_db.queries == ['DROP INDEX "some_name"']
