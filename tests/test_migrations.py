"""Alembic must be able to upgrade an empty database to head."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_alembic_upgrade_head(tmp_path: Path) -> None:
    db_file = tmp_path / "mig.db"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"url=sqlite:///{db_file.as_posix()}"]})()  # type: ignore[assignment]
    command.upgrade(cfg, "head")
    assert db_file.exists()

    # The migrated schema must match the models (no missing migration).
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.db import Base, make_engine

    engine = make_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == [], f"models and migrations differ: {diff}"

    command.downgrade(cfg, "base")
