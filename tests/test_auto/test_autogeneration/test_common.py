import datetime as dt
from pathlib import Path
from typing import Any

import peewee as pw
import pytest

from miggy.auto import MigrationAutodetector
from miggy.operations import AddField, CreateModel
from miggy.router import Router
from miggy.state import State
from tests.helpers import diff_one, operation_to_one_line


def test_on_real_migrations(migrations_dir: Path):
    router = Router("sqlite:///:memory:", migrate_dir=migrations_dir)
    router.run()
    router.build_state_from_migrations()
    state = router.state
    Person_ = state["person"]
    Tag_ = state["tag"]

    changes = MigrationAutodetector(State(), state).diff_many()
    assert len(changes) == 2
    assert all(isinstance(c, CreateModel) for c in changes)

    class Person1(pw.Model):
        first_name = pw.IntegerField()
        last_name = pw.CharField(max_length=1024, null=True, unique=True)
        tag = pw.ForeignKeyField(Tag_, on_delete="CASCADE", backref="persons")
        email = pw.CharField(index=True, unique=True)

        class Meta:
            table_name = "person"

    changes = diff_one(Person_, Person1)
    assert len(changes) == 5
    assert isinstance(changes[0], AddField)

    class Person2(pw.Model):
        first_name = pw.CharField(max_length=255)
        last_name = pw.CharField(max_length=255, index=True)
        dob = pw.DateField(null=True)
        birthday = pw.DateField(default=dt.datetime.now)
        email = pw.CharField(index=True, unique=True)

        class Meta:
            table_name = "person"

    changes = diff_one(Person2, Person_)
    assert not changes


def test_drop_field_w_constraint() -> None:
    class OldTest(pw.Model):
        first_name = pw.CharField()
        age = pw.IntegerField(constraints=[pw.SQL("DEFAULT 5")])

        class Meta:
            table_name = "test"

    class Test(pw.Model):
        first_name = pw.CharField()

        class Meta:
            table_name = "test"

    operation = diff_one(OldTest, Test)[0]
    assert operation_to_one_line(operation) == "migrator.remove_field(model_name='test',name='age',)"  # type: ignore


def test_proper_order_for_fk() -> None:
    def from_state() -> State:
        class Users(pw.Model):
            name = pw.TextField()

        return State({"users": Users})

    def to_state() -> State:
        class Test(pw.Model):
            name = pw.TextField()

        class Users(pw.Model):
            name = pw.TextField()
            test = pw.ForeignKeyField(Test, null=True, backref="users")

        return State({"test": Test, "users": Users})

    changes = MigrationAutodetector(from_state(), to_state()).diff_many()
    # we create model first
    assert isinstance(changes[0], CreateModel)
    # we add fk to it after
    assert isinstance(changes[1], AddField)


@pytest.mark.parametrize(
    ("prev", "current", "expected"),
    [
        pytest.param(
            {
                "fields": {
                    "age": pw.IntegerField(),
                    "uid": pw.IntegerField(primary_key=True),
                    "name": pw.CharField(),
                    "guid": pw.IntegerField(),
                },
                "meta": {},
            },
            {
                "fields": {
                    "age": pw.IntegerField(),
                    "uid": pw.IntegerField(),
                    "name": pw.CharField(),
                    "guid": pw.IntegerField(primary_key=True),
                },
                "meta": {},
            },
            [
                "migrator.alter_field(model_name='test',name='uid',field=pw.IntegerField(),)",
                "migrator.alter_field(model_name='test',name='guid',field=pw.IntegerField(primary_key=True),)",
            ],
            id="single_pk_to_single",
        ),
        pytest.param(
            {
                "fields": {
                    "age": pw.IntegerField(),
                    "uid": pw.IntegerField(),
                    "name": pw.CharField(),
                    "guid": pw.IntegerField(primary_key=True),
                },
                "meta": {},
            },
            {
                "fields": {
                    "age": pw.IntegerField(),
                    "uid": pw.IntegerField(primary_key=True),
                    "name": pw.CharField(),
                    "guid": pw.IntegerField(),
                },
                "meta": {},
            },
            [
                "migrator.alter_field(model_name='test',name='guid',field=pw.IntegerField(),)",
                "migrator.alter_field(model_name='test',name='uid',field=pw.IntegerField(primary_key=True),)",
            ],
            id="single_pk_to_single_reverse",
        ),
        pytest.param(
            {
                "fields": {
                    "id": pw.AutoField(),
                    "uid": pw.IntegerField(),
                },
                "meta": {},
            },
            {"fields": {"uid": pw.IntegerField(primary_key=True)}, "meta": {}},
            [
                "migrator.remove_field(model_name='test',name='id',)",
                "migrator.alter_field(model_name='test',name='uid',field=pw.IntegerField(primary_key=True),)",
            ],
            id="auto_to_single_pk",
        ),
        pytest.param(
            {
                "fields": {
                    "uid": pw.IntegerField(primary_key=True),
                },
                "meta": {},
            },
            {
                "fields": {
                    "id": pw.AutoField(),
                    "uid": pw.IntegerField(),
                },
                "meta": {},
            },
            [
                "migrator.alter_field(model_name='test',name='uid',field=pw.IntegerField(),)",
                "migrator.add_field(model_name='test',name='id',field=pw.AutoField(),)",
            ],
            id="single_pk_to_auto",
        ),
        pytest.param(
            {
                "fields": {"a": pw.CharField()},
                "meta": {"primary_key": False},
            },
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            [
                "migrator.add_field(model_name='test',name='b',field=pw.CharField(),)",
                "migrator.add_primary_key_constraint('test','a','b',)",
            ],
            id="no_pk_to_composite",
        ),
        pytest.param(
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            {
                "fields": {"b": pw.CharField()},
                "meta": {"primary_key": False},
            },
            [
                "migrator.remove_primary_key_constraint('test',)",
                "migrator.remove_field(model_name='test',name='a',)",
            ],
            id="composite_pk_to_none",
        ),
        pytest.param(
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            {
                "fields": {"a": pw.CharField(), "c": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "c")},
            },
            [
                "migrator.remove_primary_key_constraint('test',)",
                "migrator.add_field(model_name='test',name='c',field=pw.CharField(),)",
                "migrator.remove_field(model_name='test',name='b',)",
                "migrator.add_primary_key_constraint('test','a','c',)",
            ],
            id="composite_changed",
        ),
        pytest.param(
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {},
            },
            [
                "migrator.remove_primary_key_constraint('test',)",
                "migrator.add_field(model_name='test',name='id',field=pw.AutoField(),)",
            ],
            id="composite_to_auto",
        ),
        pytest.param(
            {
                "fields": {"email": pw.CharField(), "name": pw.CharField()},
                "meta": {},
            },
            {
                "fields": {"email": pw.CharField(), "name": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("email", "name")},
            },
            [
                "migrator.remove_field(model_name='test',name='id',)",
                "migrator.add_primary_key_constraint('test','email','name',)",
            ],
            id="auto_to_composite",
        ),
        pytest.param(
            {
                "fields": {"pk": pw.IntegerField(primary_key=True)},
                "meta": {},
            },
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            [
                "migrator.remove_field(model_name='test',name='pk',)",
                "migrator.add_field(model_name='test',name='a',field=pw.CharField(),)",
                "migrator.add_field(model_name='test',name='b',field=pw.CharField(),)",
                "migrator.add_primary_key_constraint('test','a','b',)",
            ],
            id="single_pk_to_composite",
        ),
        pytest.param(
            {
                "fields": {"a": pw.CharField(), "b": pw.CharField()},
                "meta": {"primary_key": pw.CompositeKey("a", "b")},
            },
            {
                "fields": {"a": pw.CharField(primary_key=True), "b": pw.CharField()},
                "meta": {},
            },
            [
                "migrator.remove_primary_key_constraint('test',)",
                "migrator.alter_field(model_name='test',name='a',field=pw.CharField(primary_key=True),)",
            ],
            id="composite_to_single_pk",
        ),
    ],
)
def test_primary_key_order(prev: dict[str, Any], current: dict[str, Any], expected: list[str]) -> None:

    from_state = State()
    from_state.add_model("Test", **prev)

    to_state = State()
    to_state.add_model("Test", **current)

    diffs = MigrationAutodetector(from_state, to_state).diff_one("test")
    diffs = [operation_to_one_line(o) for o in diffs]  # type: ignore
    assert diffs == expected


@pytest.mark.parametrize(
    ("constraints_before", "constraints_after", "changes"),
    [
        # Adding constraints
        (
            [pw.Check("first_name != 'bob'", "check_name")],
            [
                pw.Check("first_name != 'bob'", "check_name"),
                pw.Check(
                    "last_name != 'dylan'",
                    "check_lastname",
                ),
            ],
            ["migrator.add_check_constraint('test','check_lastname',\"last_name != 'dylan'\",)"],
        ),
        # Remove constraints
        (
            [
                pw.Check("first_name != 'bob'", "check_name"),
                pw.Check(
                    "last_name != 'dylan'",
                    "check_lastname",
                ),
            ],
            [
                pw.Check("first_name != 'bob'", "check_name"),
            ],
            ["migrator.remove_check_constraint('test','check_lastname',)"],
        ),
        # Nothing to do
        (
            [pw.Check("first_name != 'bob'", "check_name")],
            [pw.Check("first_name != 'bob'", "check_name")],
            [],
        ),
    ],
)
def test_check_constraints(constraints_before: list[Any], constraints_after: list[Any], changes: list[str]) -> None:
    def create_model(constraints_: list[Any]) -> type[pw.Model]:
        class Test(pw.Model):
            first_name = pw.CharField()
            last_name = pw.CharField()

            class Meta:
                constraints = constraints_

        return Test

    diffs = diff_one(create_model(constraints_before), create_model(constraints_after))
    assert [operation_to_one_line(d) for d in diffs] == changes  # type: ignore
