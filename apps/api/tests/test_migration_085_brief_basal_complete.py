"""Migration 085: nullable daily-brief basal completeness state."""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.database import get_db_session
from src.models.daily_brief import DailyBrief

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "085_brief_basal_complete.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_085", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn_name: str) -> str:
    module = _load_migration()
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf},
    )
    with Operations.context(ctx):
        getattr(module, fn_name)()
    return buf.getvalue()


def test_chains_onto_previous_head():
    module = _load_migration()
    assert module.revision == "085_brief_basal_complete"
    assert module.down_revision == "084_add_user_timezone"


def test_upgrade_adds_nullable_column_without_default():
    sql = _render("upgrade")
    assert "ALTER TABLE daily_briefs ADD COLUMN basal_estimate_complete BOOLEAN" in sql
    assert "NOT NULL" not in sql
    assert "DEFAULT" not in sql
    assert "UPDATE" not in sql.upper()


def test_downgrade_drops_column():
    sql = _render("downgrade")
    assert "ALTER TABLE daily_briefs DROP COLUMN basal_estimate_complete" in sql


def test_model_matches_migration():
    column = DailyBrief.__table__.c.basal_estimate_complete
    assert column.nullable is True
    assert column.server_default is None


async def test_migrated_table_has_nullable_column_without_default():
    async with get_db_session() as db:
        row = (
            await db.execute(
                text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'daily_briefs' "
                    "AND column_name = 'basal_estimate_complete'"
                )
            )
        ).one_or_none()
    assert row == ("boolean", "YES", None)
