from datetime import datetime


class SensorReading:
    def __init__(
        self,
        device_id: str,
        temperature: float,
        humidity: float,
        recorded_at: datetime,
        id: int | None = None,
        sequence: int | None = None,
    ):
        self.id = id
        self.device_id = device_id
        self.temperature = temperature
        self.humidity = humidity
        self.recorded_at = recorded_at
        self.sequence = sequence


class StorageThresholds:
    def __init__(
        self,
        device_id: str,
        min_temperature: float,
        max_temperature: float,
        min_humidity: float,
        max_humidity: float,
        id: int | None = None,
        is_current: bool = False,
    ):
        self.id = id
        self.device_id = device_id
        self.min_temperature = min_temperature
        self.max_temperature = max_temperature
        self.min_humidity = min_humidity
        self.max_humidity = max_humidity
        self.is_current = is_current


class ActuatorEvent:
    def __init__(
        self,
        device_id: str,
        event_type: str,
        triggered_at: datetime,
        id: int | None = None,
        resolved_at: datetime | None = None,
    ):
        self.id = id
        self.device_id = device_id
        self.event_type = event_type
        self.triggered_at = triggered_at
        self.resolved_at = resolved_at
