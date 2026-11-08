from pathlib import Path
import sys

from peewee import Model, SqliteDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATABASE_PATH = PROJECT_ROOT / "edge_clean.db"
db = SqliteDatabase(
    DATABASE_PATH,
    pragmas={
        # WAL + busy timeout let the request thread and the sync worker thread
        # read/write the SQLite file concurrently without "database is locked".
        "journal_mode": "wal",
        "busy_timeout": 5000,
    },
)

class BaseModel(Model):
    class Meta:
        database = db

def init_db():
    db.connect(reuse_if_open=True)
    from iam.infrastructure.models import DeviceModel
    from iotmonitoring.infrastructure.models import (
        ActuatorEventModel,
        BackendAccountModel,
        SensorReadingModel,
        StorageThresholdsModel,
    )
    # Migrate BEFORE create_tables: create_tables emits
    # `CREATE INDEX ... ON sensor_readings("is_synced")`, and if that column does
    # not exist yet SQLite silently treats the quoted name as a string literal
    # (corrupting the column). Adding the column first keeps the index correct.
    _ensure_outbox_columns()
    db.create_tables(
        [
            DeviceModel,
            SensorReadingModel,
            StorageThresholdsModel,
            ActuatorEventModel,
            BackendAccountModel,
        ],
        safe=True,
    )
    db.close()


def _ensure_outbox_columns():
    """Add outbox columns to pre-existing ``sensor_readings`` tables.

    create_tables(safe=True) never alters an existing table, so for databases
    created before the edge<->backend sync feature we add the columns by hand.
    Brand-new databases have no table yet; create_tables handles those.
    """
    if "sensor_readings" not in db.get_tables():
        return
    cursor = db.execute_sql("PRAGMA table_info(sensor_readings)")
    existing = {row[1] for row in cursor.fetchall()}
    if "is_synced" not in existing:
        db.execute_sql(
            "ALTER TABLE sensor_readings ADD COLUMN is_synced INTEGER NOT NULL DEFAULT 0"
        )
    if "synced_at" not in existing:
        db.execute_sql("ALTER TABLE sensor_readings ADD COLUMN synced_at DATETIME")
    if "sequence" not in existing:
        db.execute_sql(
            "ALTER TABLE sensor_readings ADD COLUMN sequence INTEGER NOT NULL DEFAULT 1"
        )

if __name__ == "__main__":
    init_db()
