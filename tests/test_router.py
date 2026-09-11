import os
import pathlib
from textwrap import dedent
from unittest import mock

import peewee as pw
import playhouse
import pytest
from playhouse.postgres_ext import Psycopg3Database

from miggy.router import Router, detect_changes, get_router
from miggy.state import State
from tests.conftest import POSTGRES_DSN
from tests.helpers import get_active_status


def test_router_run_already_applied_ok(router: Router) -> None:
    router.run()
    router.build_state_from_migrations()
    Person = router.state["person"]

    assert Person.get_or_none(email="person@example.com") is not None

    Person.delete().execute()

    router.run_one("004_test_insert")
    assert Person.get_or_none(email="person@example.com") is None


def test_router_todo_diff_done(router: Router, migrations_dir: pathlib.Path):
    MigrateHistory = router.model

    assert router.todo == ["001_test", "002_test", "003_tespy", "004_test_insert"]
    assert router.done == []
    assert router.diff == ["001_test", "002_test", "003_tespy", "004_test_insert"]

    router.create("new")
    assert router.todo == ["001_test", "002_test", "003_tespy", "004_test_insert", "005_new"]
    os.remove(os.path.join(migrations_dir, "005_new.py"))

    MigrateHistory.create(name="001_test")
    assert router.diff == ["002_test", "003_tespy", "004_test_insert"]
    MigrateHistory.delete().execute()


def test_router_rollback(router: Router):
    MigrateHistory = router.model
    router.run()

    migrations = MigrateHistory.select()
    assert list(migrations)
    assert migrations.count() == 4

    router.rollback("004_test_insert")
    router.rollback("003_tespy")
    assert router.diff == ["003_tespy", "004_test_insert"]
    assert migrations.count() == 2


def test_router_merge(router: Router, migrations_dir: pathlib.Path):
    MigrateHistory = router.model
    router.run()

    with mock.patch("os.remove") as mocked:
        router.merge()
        assert mocked.call_count == 4
        assert mocked.call_args[0][0] == os.path.join(migrations_dir, "004_test_insert.py")
        assert MigrateHistory.select().count() == 1

    # after merge we have new migration, remove it for cleanup purposes
    os.remove(os.path.join(migrations_dir, "001_initial.py"))


def test_router_schema(tmpdir):
    schema_name = "test"
    migrations = tmpdir.mkdir("migrations")

    with mock.patch("miggy.router.Router.done"):
        router = Router(database="postgres:///fake", migrate_dir=str(migrations), schema=schema_name)

        assert router.schema == schema_name
        # TODO: test schema change


@pytest.mark.parametrize(
    ("migration_name", "expected"),
    [
        ("w_transaction", True),
        ("wo_transaction", False),
    ],
)
def test_migration_atomic(resources_dir: pathlib.Path, expected: bool, migration_name: str) -> None:
    db = playhouse.db_url.connect("sqlite:///:memory:")
    with mock.patch.object(db, "transaction") as mocked:
        router = Router(
            db,
            migrate_dir=resources_dir / "transaction_test",
        )
        router.run_one(migration_name, change_schema=True, change_history=True)
        transaction_called = mocked.call_count == 1
        assert transaction_called is expected


def test_compile(tmp_path: pathlib.Path) -> None:
    def from_state() -> State:
        class Test(pw.Model):
            first_name = pw.CharField()

            class Meta:
                table_name = "test"

        return State({"test": Test})

    def _to_state() -> State:
        class Test(pw.Model):
            first_name = pw.CharField(default=get_active_status)
            field = pw.IntegerField(constraints=[pw.SQL("DEFAULT 5")])

            class Meta:
                table_name = "test"

        return State({"test": Test})

    changes = detect_changes(from_state(), _to_state())

    d = tmp_path / "migrations"
    d.mkdir()
    router = Router(Psycopg3Database(POSTGRES_DSN), migrate_dir=d)
    router.compile("test_router_compile", changes, [])

    with open(d / "001_test_router_compile.py") as f:
        content = f.read()
        assert (
            dedent(
                '''
        def migrate(migrator, database, fake=False):
            """Write your migrations here."""

            migrator.add_field(
                model_name='test',
                name='field',
                field=pw.IntegerField(constraints=[pw.SQL('DEFAULT 5')]),
            )

            migrator.alter_field(
                model_name='test',
                name='first_name',
                field=pw.CharField(default=tests.helpers.get_active_status),
            )


        def rollback(migrator, database, fake=False):
            """Write your rollback migrations here."""
        '''
            )
            in content
        )


def test_get_router_reads_config(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "db.sqlite3"
    db_path.touch()
    conf = tmp_path / "miggyconf.py"
    conf.write_text(
        "DATABASE = 'sqlite:///%s'\n"
        "IGNORE = ['ignored_model']\n"
        "SCHEMA = 'config_schema'\n"
        "MIGRATE_TABLE = 'custom_history'\n"
        "MIGRATE_DIR = 'custom_migrations'"
    )

    router = get_router("cli_dir", "sqlite:///:memory:", "cli_schema", 0, conf_path=conf)

    assert router.ignore == ["ignored_model"]
    assert router.schema == "config_schema"
    assert router.migrate_table == "custom_history"
    assert router.migrate_dir == tmp_path / "custom_migrations"
    assert router.working_dir == tmp_path


def test_get_router_defaults_without_conf_path() -> None:
    directory = "migrations"

    with pytest.warns(DeprecationWarning, match="conf_path=None"):
        router = get_router(directory, "sqlite:///:memory:")

    assert isinstance(router, Router)
    assert router.ignore == []
    assert router.schema is None
    assert router.migrate_table == "migratehistory"
    assert router.migrate_dir == pathlib.Path(os.getcwd()) / directory
    assert router.working_dir == pathlib.Path(os.getcwd())


def test_get_router_falls_back_to_legacy_conf_py(tmp_path: pathlib.Path) -> None:
    directory = tmp_path / "some_dir"
    directory.mkdir()
    (directory / "conf.py").write_text("MIGRATE_TABLE = 'legacy_history'\n")

    with pytest.warns(DeprecationWarning, match="conf_path=None"):
        router = get_router(directory, "sqlite:///:memory:")

    assert router.migrate_table == "legacy_history"
    assert router.migrate_dir == pathlib.Path(os.getcwd()) / directory
    assert router.working_dir == pathlib.Path(os.getcwd())


def test_get_router_sys_exit() -> None:
    with pytest.warns(DeprecationWarning, match="conf_path=None"):
        with pytest.raises(SystemExit) as exc_info:
            get_router(str("migrations"), "unknown://foo")

    assert exc_info.value.code == 1
