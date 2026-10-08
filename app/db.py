import logging
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine, Session
from app.config import DB_PATH

# timeout: how long a query waits for another connection's lock (SQLite's default
# is 5s). Background jobs (scan, tagging, matching) write while the UI polls them,
# so waiting a little longer beats surfacing "database is locked".
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30})
logger = logging.getLogger("modelhub.db")

_SQLITE_TYPES = {"INTEGER": "INTEGER", "VARCHAR": "TEXT", "BOOLEAN": "BOOLEAN",
                  "FLOAT": "FLOAT", "DATETIME": "DATETIME", "BLOB": "BLOB"}


def _sync_columns():
    """SQLModel's create_all() only creates missing tables, not missing columns on
    tables that already exist. This adds any new model columns via ALTER TABLE so
    upgrading the container doesn't wipe or break an existing library database."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = _SQLITE_TYPES.get(col.type.__class__.__name__.upper(), "TEXT")
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}'))
                logger.info("Migrated: added column %s.%s", table_name, col.name)


def init_db():
    SQLModel.metadata.create_all(engine)
    _sync_columns()


def get_session():
    with Session(engine) as session:
        yield session
