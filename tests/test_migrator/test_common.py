from collections.abc import Callable

import peewee as pw
import pytest
from playhouse.db_url import connect
from playhouse.migrate import Operation

from miggy import Migrator, types
from miggy.operations import MigrateOperation
from miggy.schema import SchemaMigrator
from miggy.state import State
from tests.conftest import PatchedPgDatabase


def test_migrator_sqlite_common():
    database = connect("sqlite:///:memory:")
    migrator = Migrator(database)

    @migrator.create_table
    class Customer(pw.Model):
        name = pw.CharField()

    @migrator.create_table
    class Order(pw.Model):
        number = pw.CharField()
        uid = pw.CharField(unique=True)

        customer_id = pw.ForeignKeyField(Customer, column_name="customer_id")

    migrator.run()

    Customer = migrator.state["customer"]
    Order = migrator.state["order"]  # noqa: F811

    migrator.add_fields("order", finished=pw.BooleanField(default=False))
    migrator.run()
    assert "finished" in Order._meta.fields

    migrator.drop_columns("order", "finished", "customer_id", "uid")
    migrator.run()
    assert "finished" not in Order._meta.fields
    assert not hasattr(Order, "customer_id")
    assert not hasattr(Order, "customer_id_id")

    migrator.add_fields("order", customer=pw.ForeignKeyField(Customer, null=True))
    migrator.run()

    assert "customer" in Order._meta.fields
    assert Order.customer.name == "customer"
    assert Order.customer.name == "customer"

    migrator.rename_field("Order", "number", "identifier")
    migrator.run()

    assert "identifier" in Order._meta.fields

    migrator.drop_not_null("Order", "identifier")
    migrator.run()
    assert Order._meta.fields["identifier"].null
    assert Order._meta.columns["identifier"].null

    migrator.change_columns("Order", identifier=pw.IntegerField(default=0))
    migrator.run()

    assert Order.identifier.field_type == "INT"

    Order.create(identifier=55)
    migrator.sql('UPDATE "order" SET identifier = 77;')
    migrator.run()

    order = Order.get()
    assert order.identifier == 77

    migrator.add_index("Order", "identifier", "customer", name="some_name")
    migrator.run()
    assert Order._meta.indexes_state
    assert not Order.identifier.index

    migrator.drop_index("Order", "some_name")
    migrator.run()
    assert not Order._meta.indexes_state

    migrator.remove_fields("order", "customer")
    migrator.run()
    assert not hasattr(Order, "customer")
    migrator.add_index("Order", "identifier", unique=True, name="some_name")
    migrator.run()

    assert Order._meta.indexes_state

    migrator.rename_table("order", "new_name")
    migrator.run()
    assert Order._meta.table_name == "new_name"
    migrator.rename_table("order", "order")
    migrator.run()
    assert Order._meta.table_name == "order"


@pytest.mark.parametrize(
    ("fields", "name", "unique", "where", "expected"),
    [
        (
            ("name",),
            "user_name",
            False,
            None,
            'CREATE INDEX "user_name" ON "user" ("name")',
        ),
        (
            ("name", "created_at"),
            "user_name_created_at",
            True,
            None,
            'CREATE UNIQUE INDEX "user_name_created_at" ON "user" ("name", "created_at")',
        ),
        (
            ("name",),
            "some_name",
            False,
            pw.SQL("name = 'John'"),
            """CREATE INDEX "some_name" ON "user" ("name") WHERE name = 'John'""",
        ),
    ],
)
def test_migrator_add_index(
    patched_pg_db: PatchedPgDatabase,
    fields: tuple[str, ...],
    name: str,
    unique: bool,
    where: pw.SQL | None,
    expected: str,
) -> None:
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        name = pw.CharField()
        created_at = pw.DateField()

    migrator.run()
    patched_pg_db.clear_queries()

    migrator.add_index("user", *fields, name=name, unique=unique, where=where)
    migrator.run()
    assert patched_pg_db.queries == [expected]


def test_migrator_schema(patched_pg_db: PatchedPgDatabase):
    schema_name = "test_schema"
    patched_pg_db.execute_sql("DROP SCHEMA IF EXISTS test_schema CASCADE;")
    patched_pg_db.execute_sql("CREATE SCHEMA  test_schema;")

    migrator = Migrator(patched_pg_db, schema=schema_name)

    patched_pg_db.clear_queries()

    @migrator.create_table
    class User(types.Model):
        name = pw.CharField()
        created_at = pw.DateField()

    migrator.run()

    assert patched_pg_db.queries[0] == f'SET search_path TO "{schema_name}"'

    patched_pg_db.clear_queries()
    migrator.change_fields("user", created_at=pw.DateTimeField())
    migrator.run()

    assert patched_pg_db.queries[0] == f'SET search_path TO "{schema_name}"'
    patched_pg_db.execute_sql("DROP SCHEMA test_schema CASCADE;")


def test_run_python(patched_pg_db: PatchedPgDatabase):
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        first_name = pw.CharField()
        last_name = pw.CharField()

    migrator.run()
    patched_pg_db.clear_queries()

    def save_user(schema_migrator, state):
        User = state["user"]
        User(
            first_name="First",
            last_name="Last",
        ).save()

    migrator.python(save_user)

    migrator.run()

    assert patched_pg_db.queries == [
        'INSERT INTO "user" ("first_name", "last_name") VALUES (First, Last) RETURNING "user"."id"',
    ]


def test_run_sql(patched_pg_db: PatchedPgDatabase):
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        first_name = pw.CharField()
        last_name = pw.CharField()

    migrator.run()
    patched_pg_db.clear_queries()

    migrator.sql(
        """INSERT INTO "user" ("first_name", "last_name") VALUES ('First', 'Last')""",
    )

    migrator.run()

    assert migrator.state["user"].get(first_name="First", last_name="Last") is not None


def test_run_sql_w_params(patched_pg_db: PatchedPgDatabase):
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        first_name = pw.CharField()
        last_name = pw.CharField()

    migrator.run()
    patched_pg_db.clear_queries()

    migrator.sql(
        """INSERT INTO "user" ("first_name", "last_name") VALUES (%s, %s)""",
        (
            "First",
            "Last",
        ),
    )

    migrator.run()

    assert migrator.state["user"].get(first_name="First", last_name="Last") is not None


def test_add_operation(patched_pg_db: PatchedPgDatabase) -> None:
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        first_name = pw.CharField()
        last_name = pw.CharField()

    migrator.run()
    patched_pg_db.clear_queries()

    class MyOperation(MigrateOperation):
        def state_forwards(self, state: State) -> None:
            state.remove_field("user", "last_name")

        def database_forwards(
            self, schema_migrator: SchemaMigrator, from_state: State, to_state: State
        ) -> list[Operation] | list[Callable]:
            return [schema_migrator.drop_column("user", "last_name")]

    migrator.add_operation(MyOperation())

    migrator.run()

    assert not hasattr(migrator.state["User"], "last_name")
    assert patched_pg_db.queries[-1] == 'ALTER TABLE "user" DROP COLUMN "last_name" CASCADE'


def test_rename_table(patched_pg_db: PatchedPgDatabase) -> None:
    migrator = Migrator(patched_pg_db)

    @migrator.create_table
    class User(pw.Model):
        first_name = pw.CharField(unique=True)
        last_name = pw.CharField(index=True)

    migrator.run()
    patched_pg_db.clear_queries()

    migrator.rename_table("user", "new_name")
    migrator.run()

    assert migrator.state["User"]._meta.table_name == "new_name"

    _, _, rename_table, _, rename_seq, rename_index1, rename_index2 = patched_pg_db.queries

    assert [rename_table, rename_seq, rename_index1, rename_index2] == [
        'ALTER TABLE "user" RENAME TO "new_name"',
        'ALTER TABLE "user_id_seq" RENAME TO "new_name_id_seq"',
        'ALTER INDEX "user_first_name" RENAME TO "new_name_first_name"',
        'ALTER INDEX "user_last_name" RENAME TO "new_name_last_name"',
    ]
