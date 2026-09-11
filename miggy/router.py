import os
import pkgutil
import re
import sys
import typing
from functools import cached_property
from importlib import import_module
from logging import Logger
from pathlib import Path
from typing import Any

import peewee as pw
from playhouse.db_url import connect

from miggy import LOGGER, MigrateHistory
from miggy.auto import NEWLINE, MigrationAutodetector
from miggy.migrator import Migrator
from miggy.operations import MigrateOperation
from miggy.state import State
from miggy.utils import MIGRATION_TEMPLATE, deprecated_warn, exec_in
from miggy.writer import OperationWriter

CLEAN_RE = re.compile(r"\s+$", re.M)
DEFAULT_MIGRATE_DIR = "migrations"
UNDEFINED = object()
VOID = lambda m, d: None  # noqa


class Migration:
    atomic = True

    @staticmethod
    def migrate(migrator, database, fake=False) -> None:
        pass

    @staticmethod
    def rollback(migrator, database, fake=False) -> None:
        pass


def add_to_sys_path(directory: str | Path) -> None:
    if directory not in sys.path:
        sys.path.insert(0, str(directory))


class Router(object):
    """Abstract base class for router."""

    filemask = re.compile(r"[\d]{3}_[^\.]+\.py$")

    def __init__(
        self,
        database: str | pw.Database | pw.Proxy | None,
        migrate_table: str = "migratehistory",
        migrate_dir: str | Path = "migrations",
        ignore: list[str] | None = None,
        schema: str | None = None,
        logger: Logger = LOGGER,
        working_dir: str | Path | None = None,
    ) -> None:
        if isinstance(database, str):
            database = connect(database)
        if not isinstance(database, (pw.Database, pw.Proxy)):
            raise RuntimeError("Invalid database: %s" % database)
        self.database = database
        working_dir = working_dir or os.getcwd()
        self.working_dir = Path(working_dir)
        # Need to append the working_dir to the path for import to work.
        add_to_sys_path(self.working_dir)
        self.migrate_dir = self.working_dir / migrate_dir
        self.migrate_table = migrate_table
        self.schema = schema
        self.ignore = ignore or []
        self.logger = logger
        self.migration_template = MIGRATION_TEMPLATE.read_text()

    @cached_property
    def model(self) -> typing.Type[MigrateHistory]:
        """Initialize and cache MigrationHistory model."""
        MigrateHistory._meta.database = self.database
        MigrateHistory._meta.table_name = self.migrate_table
        MigrateHistory._meta.schema = self.schema
        MigrateHistory.create_table(True)
        return MigrateHistory

    @property
    def todo(self):
        """Scan migrations in file system."""
        if not os.path.exists(self.migrate_dir):
            self.logger.warning("Migration directory: %s does not exist.", self.migrate_dir)
            os.makedirs(self.migrate_dir)
        return sorted(f[:-3] for f in os.listdir(self.migrate_dir) if self.filemask.match(f))

    @property
    def done(self):
        """Scan migrations in database."""
        return [mm.name for mm in self.model.select().order_by(self.model.id)]

    @property
    def diff(self):
        """Calculate difference between fs and db."""
        done = set(self.done)
        return [name for name in self.todo if name not in done]

    @cached_property
    def migrator(self):
        """Create migrator and setup it with fake migrations."""
        migrator = Migrator(self.database, self.schema)
        for name in self.done:
            self.run_one(name, migrator)
        return migrator

    @property
    def migration_state(self) -> State:
        """Create migrator and setup it with fake migrations."""
        return self.migrator.state

    def load_project_state(self, auto) -> State:
        modules = [auto]
        if isinstance(auto, bool):
            modules = [m for _, m, ispkg in pkgutil.iter_modules([str(self.working_dir)]) if ispkg]

        models = [m for module in modules for m in load_models(module)]

        return State({m._meta.name: m for m in models if m._meta.name not in self.ignore})

    def create(self, name="auto", auto=False):
        """Create a migration.
        :param auto: Python module path to scan for models.
        """
        migrate_changes = []
        rollback_changes = []
        if auto:
            try:
                project_state = self.load_project_state(auto)
            except ImportError:
                return self.logger.exception("Can't import models module")

            for migration in self.diff:
                self.run_one(migration, self.migrator)

            migrate_changes = detect_changes(self.migration_state, project_state)
            if not migrate_changes:
                return self.logger.warning("No changes found.")

            rollback_changes = detect_changes(project_state, self.migration_state)

        self.logger.info('Creating migration "%s"', name)
        name = self.compile(name, migrate_changes, rollback_changes)
        self.logger.info('Migration has been created as "%s"', name)
        return name

    def merge(self, name="initial"):
        """Merge migrations into one."""
        migrator = Migrator(self.database)
        migrate_changes = detect_changes(migrator.state, self.migration_state)
        if not migrate_changes:
            return self.logger.error("Can't merge migrations")

        self.clear()

        self.logger.info('Merge migrations into "%s"', name)
        rollback_changes = detect_changes(self.migration_state, State())
        name = self.compile(name, migrate_changes, rollback_changes, 0)

        migrator = Migrator(self.database)
        self.run_one(name, migrator, change_schema=False, change_history=True)
        self.logger.info('Migrations has been merged into "%s"', name)

    def clear(self):
        """Clear migrations."""
        self.model.delete().execute()

        # Remove migrations from fs
        for name in self.todo:
            filename = os.path.join(self.migrate_dir, name + ".py")
            os.remove(filename)

    def _serialize_changes(self, changes: list[MigrateOperation]):
        imports = set()
        serialized_changes = []
        for c in changes:
            writer = OperationWriter(c)
            serialized_changes.append(writer.serialize())
            imports.update(writer.imports)

        return  "\n".join(serialized_changes), imports

    def _compile_template(
        self, name: str, migrate_changes: list[MigrateOperation], rollback_changes: list[MigrateOperation]
    ) -> str:
        migrate, imports = self._serialize_changes(migrate_changes)
        rollback, rollback_imports = self._serialize_changes(rollback_changes)
        imports.update(rollback_imports)

        return self.migration_template.format(migrate=migrate, rollback=rollback, name=name, imports="\n".join(imports))

    def compile(
        self, name, migrate_changes: list[MigrateOperation], rollback_changes: list[MigrateOperation], num=None
    ) -> str:
        """Create a migration."""

        if num is None:
            num = len(self.todo)

        name = f"{num + 1:03}_{name}"
        filename = f"{name}.py"
        path = os.path.join(self.migrate_dir, filename)
        template = self._compile_template(filename, migrate_changes=migrate_changes, rollback_changes=rollback_changes)
        with open(path, "w") as f:
            f.write(template)

        return name

    def read(self, name):
        """Read migration from file."""
        call_params = {}
        if os.name == "nt" and sys.version_info >= (3, 0):
            # if system is windows - force utf-8 encoding
            call_params["encoding"] = "utf-8"
        with open(os.path.join(self.migrate_dir, name + ".py"), **call_params) as f:
            code = f.read()
            scope = {}
            exec_in(code, scope)

            atomic, migrate, rollback = (
                scope.get("__ATOMIC", True),
                scope.get("migrate", VOID),
                scope.get("rollback", VOID),
            )

            class _Migration(Migration):
                pass

            _Migration.atomic = atomic
            _Migration.migrate = migrate
            _Migration.rollback = rollback
            return _Migration

    def run_one(
        self,
        name: str,
        migrator: Migrator,
        change_schema: bool = False,
        change_history: bool = False,
        downgrade: bool = False,
    ) -> None:
        """Run/emulate a migration with given name."""
        fake = not change_schema
        try:
            migration = self.read(name)

            def run_migrator():
                if not downgrade:
                    self.logger.info('Migrate "%s"', name)
                    migration.migrate(migrator, self.database, fake=fake)
                    migrator.run(change_schema)
                    if change_history:
                        self.model.create(name=name)
                else:
                    self.logger.info("Rolling back %s", name)
                    migration.rollback(migrator, self.database, fake=fake)
                    migrator.run(change_schema)
                    if change_history:
                        self.model.delete().where(self.model.name == name).execute()

                self.logger.info("Done %s", name)

            if migration.atomic and change_schema:
                with self.database.transaction():
                    run_migrator()
            else:
                run_migrator()

        except Exception:
            operation = "Migration" if not downgrade else "Rollback"
            self.logger.exception("%s failed: %s", operation, name)
            raise

    def run(self, name=None, fake=False):
        """Run migrations."""
        self.logger.info("Starting migrations")

        done = []
        diff = self.diff
        if not diff:
            self.logger.info("There is nothing to migrate")
            return done

        migrator = self.migrator
        for mname in diff:
            self.run_one(mname, migrator, change_schema=not fake, change_history=True)
            done.append(mname)
            if name and name == mname:
                break

        return done

    def rollback(self, name):
        name = name.strip()
        done = self.done
        if not done:
            raise RuntimeError("No migrations are found.")
        if name != done[-1]:
            raise RuntimeError("Only last migration can be canceled.")

        migrator = self.migrator
        self.run_one(name, migrator, change_schema=True, downgrade=True, change_history=True)
        self.logger.warning("Downgraded migration: %s", name)


def load_models(module):
    """Load models from given module."""
    modules = _import_submodules(module)
    return {m for module in modules for m in filter(_check_model, (getattr(module, name) for name in dir(module)))}


def _import_submodules(package, passed=UNDEFINED):
    if passed is UNDEFINED:
        passed = set()

    if isinstance(package, str):
        package = import_module(package)

    modules = []

    for _loader, name, is_pkg in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if name in passed:
            continue
        passed.add(name)

        module = sys.modules.get(name)
        if module is None:
            module = import_module(name)

        modules.append(module)
        if is_pkg:
            modules += _import_submodules(module, passed=passed)

    return modules


def _check_model(obj, models=None):
    """Checks object if it's a peewee model and unique."""
    return isinstance(obj, type) and issubclass(obj, pw.Model) and hasattr(obj, "_meta")


def detect_changes(
    from_state: State,
    to_state: State,
) -> list[MigrateOperation]:
    return MigrationAutodetector(from_state, to_state).changes()


def get_router(directory, database, schema=None, verbose=0, conf_path: Path | None = None) -> Router:
    VERBOSE = ["WARNING", "INFO", "DEBUG", "NOTSET"]
    logging_level = VERBOSE[verbose]
    config: dict[str, Any] = {}
    migrate_table = "migratehistory"
    working_directory = os.getcwd()
    migrate_dir = directory
    ignore = None

    if conf_path:
        working_directory = conf_path.parent.as_posix()
    else:
        deprecated_warn("Calling get_router() with conf_path=None is deprecated. Please provide a conf_path.")
        conf_path = Path(directory) / "conf.py"

    if conf_path.exists():
        # for imports in config
        add_to_sys_path(working_directory)
        with open(conf_path) as cfg:
            exec_in(cfg.read(), config, config)
            database = config.get("DATABASE", database)
            ignore = config.get("IGNORE", ignore)
            schema = config.get("SCHEMA", schema)
            migrate_table = config.get("MIGRATE_TABLE", migrate_table)
            migrate_dir = config.get("MIGRATE_DIR", migrate_dir)
            logging_level = config.get("LOGGING_LEVEL", logging_level).upper()

    LOGGER.setLevel(logging_level)

    try:
        return Router(
            database,
            migrate_table=migrate_table,
            migrate_dir=migrate_dir,
            ignore=ignore,
            schema=schema,
            working_dir=working_directory,
        )
    except RuntimeError as exc:
        LOGGER.error(exc)
        return sys.exit(1)
