from peewee import AutoField, BooleanField, CharField, DateTimeField, FloatField, IntegerField

from shared.infrastructure.database import BaseModel


class SensorReadingModel(BaseModel):
    id = AutoField()
    device_id = CharField(index=True)
    temperature = FloatField()
    humidity = FloatField()
    recorded_at = DateTimeField(index=True)
    sequence = IntegerField(default=1, index=True)
    # Outbox flags for edge -> backend synchronization.
    is_synced = BooleanField(default=False, index=True)
    synced_at = DateTimeField(null=True)

    class Meta:
        table_name = "sensor_readings"


class StorageThresholdsModel(BaseModel):
    id = AutoField()
    device_id = CharField(index=True)
    min_temperature = FloatField()
    max_temperature = FloatField()
    min_humidity = FloatField()
    max_humidity = FloatField()
    is_current = BooleanField(default=True, index=True)

    class Meta:
        table_name = "storage_thresholds"


class ActuatorEventModel(BaseModel):
    id = AutoField()
    device_id = CharField(index=True)
    event_type = CharField()
    triggered_at = DateTimeField(index=True)
    resolved_at = DateTimeField(null=True)

    class Meta:
        table_name = "actuator_events"


class BackendAccountModel(BaseModel):
    # Cuenta CafeLab con la que el usuario vincula este edge al backend.
    # Reemplaza las env vars BACKEND_SERVICE_*. Fila única.
    id = AutoField()
    base_url = CharField()
    email = CharField()
    password = CharField()
    updated_at = DateTimeField()

    class Meta:
        table_name = "backend_account"
