from datetime import datetime, timezone

from dateutil.parser import parse

from iotmonitoring.domain.entities import ActuatorEvent, SensorReading, StorageThresholds

STATUS_OPTIMAL = "OPTIMAL"
STATUS_WARNING = "WARNING"
STATUS_DANGER = "DANGER"

ACTUATOR_ACTIVATE = "ACTIVATE"
ACTUATOR_DEACTIVATE = "DEACTIVATE"
ACTUATOR_NONE = "NONE"

MIN_PHYSICAL_TEMPERATURE = -40.0
MAX_PHYSICAL_TEMPERATURE = 80.0
MIN_PHYSICAL_HUMIDITY = 0.0
MAX_PHYSICAL_HUMIDITY = 100.0

MIN_ALLOWED_THRESHOLD_TEMPERATURE = 0.0
MAX_ALLOWED_THRESHOLD_TEMPERATURE = 150.0
MIN_ALLOWED_THRESHOLD_HUMIDITY = 0.0
MAX_ALLOWED_THRESHOLD_HUMIDITY = 150.0


def _require_device_id(device_id: str) -> None:
    if not device_id or not isinstance(device_id, str):
        raise ValueError("device_id must be a non-empty string")


def _to_float(value, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be numeric")


def _to_datetime(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed_value = value
    else:
        try:
            parsed_value = parse(value)
        except (TypeError, ValueError):
            raise ValueError("recorded_at must be a valid ISO 8601 datetime")

    if parsed_value.tzinfo is None:
        return parsed_value.replace(tzinfo=timezone.utc)
    return parsed_value.astimezone(timezone.utc)


class SensorReadingService:
    @staticmethod
    def create_reading(
        device_id: str,
        temperature,
        humidity,
        recorded_at=None,
        id: int | None = None,
        sequence: int = 1,
    ) -> SensorReading:
        _require_device_id(device_id)
        temperature = _to_float(temperature, "temperature")
        humidity = _to_float(humidity, "humidity")

        if not MIN_PHYSICAL_TEMPERATURE <= temperature <= MAX_PHYSICAL_TEMPERATURE:
            raise ValueError("temperature is outside the physical sensor range")
        if not MIN_PHYSICAL_HUMIDITY <= humidity <= MAX_PHYSICAL_HUMIDITY:
            raise ValueError("humidity is outside the physical sensor range")

        return SensorReading(device_id, temperature, humidity, _to_datetime(recorded_at), id, sequence)


class StorageThresholdService:
    @staticmethod
    def synchronize(
        device_id: str,
        min_temperature,
        max_temperature,
        min_humidity,
        max_humidity,
        id: int | None = None,
    ) -> StorageThresholds:
        _require_device_id(device_id)
        min_temperature = _to_float(min_temperature, "min_temperature")
        max_temperature = _to_float(max_temperature, "max_temperature")
        min_humidity = _to_float(min_humidity, "min_humidity")
        max_humidity = _to_float(max_humidity, "max_humidity")

        if min_temperature > max_temperature:
            raise ValueError("min_temperature must be lower than or equal to max_temperature")
        if min_humidity > max_humidity:
            raise ValueError("min_humidity must be lower than or equal to max_humidity")
        if not (
            MIN_ALLOWED_THRESHOLD_TEMPERATURE
            <= min_temperature
            <= max_temperature
            <= MAX_ALLOWED_THRESHOLD_TEMPERATURE
        ):
            raise ValueError(
                f"temperature thresholds must be between "
                f"{MIN_ALLOWED_THRESHOLD_TEMPERATURE:g} and "
                f"{MAX_ALLOWED_THRESHOLD_TEMPERATURE:g} Celsius"
            )
        if not (
            MIN_ALLOWED_THRESHOLD_HUMIDITY
            <= min_humidity
            <= max_humidity
            <= MAX_ALLOWED_THRESHOLD_HUMIDITY
        ):
            raise ValueError(
                f"humidity thresholds must be between "
                f"{MIN_ALLOWED_THRESHOLD_HUMIDITY:g} and "
                f"{MAX_ALLOWED_THRESHOLD_HUMIDITY:g} percent"
            )

        return StorageThresholds(
            device_id,
            min_temperature,
            max_temperature,
            min_humidity,
            max_humidity,
            id,
            True,
        )

    @staticmethod
    def sincronize(
        device_id: str,
        min_temperature,
        max_temperature,
        min_humidity,
        max_humidity,
        id: int | None = None,
    ) -> StorageThresholds:
        return StorageThresholdService.synchronize(
            device_id,
            min_temperature,
            max_temperature,
            min_humidity,
            max_humidity,
            id,
        )


class StorageConditionService:
    @staticmethod
    def evaluate(reading: SensorReading, thresholds: StorageThresholds) -> str:
        temp_out_of_range = (
            reading.temperature > thresholds.max_temperature
            or reading.temperature < thresholds.min_temperature
        )
        if reading.humidity > thresholds.max_humidity or temp_out_of_range:
            return STATUS_DANGER
        if reading.humidity < thresholds.min_humidity:
            return STATUS_WARNING
        return STATUS_OPTIMAL

    @staticmethod
    def actuator_command(reading: SensorReading, thresholds: StorageThresholds) -> str:
        if reading.humidity > thresholds.max_humidity:
            return ACTUATOR_ACTIVATE
        if reading.humidity <= thresholds.max_humidity:
            return ACTUATOR_DEACTIVATE
        return ACTUATOR_NONE

    @staticmethod
    def environmental_alerts(
        reading: SensorReading, thresholds: StorageThresholds
    ) -> tuple[bool, bool]:
        """Devuelve (humidity_alert, temperature_alert).

        humidity_alert es True únicamente cuando reading.humidity > thresholds.max_humidity.
        temperature_alert es True cuando la temperatura esta fuera de rango (< min o > max).
        """
        humidity_alert = reading.humidity > thresholds.max_humidity
        temperature_alert = (
            reading.temperature > thresholds.max_temperature
            or reading.temperature < thresholds.min_temperature
        )
        return humidity_alert, temperature_alert


class ActuatorEventService:
    @staticmethod
    def create_event(
        device_id: str,
        event_type: str,
        triggered_at=None,
        id: int | None = None,
        resolved_at=None,
    ) -> ActuatorEvent:
        _require_device_id(device_id)
        if event_type not in (ACTUATOR_ACTIVATE, ACTUATOR_DEACTIVATE):
            raise ValueError("event_type must be ACTIVATE or DEACTIVATE")

        return ActuatorEvent(
            device_id,
            event_type,
            _to_datetime(triggered_at),
            id,
            _to_datetime(resolved_at) if resolved_at is not None else None,
        )

    @staticmethod
    def activate(device_id: str, triggered_at=None, id: int | None = None) -> ActuatorEvent:
        return ActuatorEventService.create_event(
            device_id,
            ACTUATOR_ACTIVATE,
            triggered_at,
            id,
        )

    @staticmethod
    def deactivate(
        device_id: str,
        triggered_at=None,
        id: int | None = None,
        resolved_at=None,
    ) -> ActuatorEvent:
        return ActuatorEventService.create_event(
            device_id,
            ACTUATOR_DEACTIVATE,
            triggered_at,
            id,
            resolved_at,
        )
