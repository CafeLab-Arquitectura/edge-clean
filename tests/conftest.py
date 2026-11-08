import pytest
from peewee import SqliteDatabase

from iam.infrastructure.models import DeviceModel
from iotmonitoring.infrastructure.models import (
    ActuatorEventModel,
    BackendAccountModel,
    SensorReadingModel,
    StorageThresholdsModel,
)
from shared.infrastructure.database import db

MODELS = [
    DeviceModel,
    SensorReadingModel,
    StorageThresholdsModel,
    ActuatorEventModel,
    BackendAccountModel,
]


@pytest.fixture(autouse=True)
def test_db():
    test_database = SqliteDatabase(":memory:")
    with test_database.bind_ctx(MODELS):
        test_database.create_tables(MODELS)
        yield test_database
        test_database.drop_tables(MODELS)

