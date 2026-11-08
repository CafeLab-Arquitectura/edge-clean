from datetime import datetime, timezone

from iam.infrastructure.repositories import DeviceRepository
from iotmonitoring.domain.entities import ActuatorEvent, SensorReading, StorageThresholds
from iotmonitoring.domain.services import (
    ACTUATOR_ACTIVATE,
    ACTUATOR_DEACTIVATE,
    SensorReadingService,
    StorageConditionService,
    StorageThresholdService,
    ActuatorEventService,
)
from iotmonitoring.infrastructure.repositories import (
    ActuatorEventRepository,
    SensorReadingRepository,
    StorageThresholdsRepository,
)

DEFAULT_MIN_TEMPERATURE = 0.0
DEFAULT_MAX_TEMPERATURE = 150.0
DEFAULT_MIN_HUMIDITY = 0.0
DEFAULT_MAX_HUMIDITY = 150.0


class IoTMonitoringApplicationService:
    def __init__(self):
        self.device_repository = DeviceRepository()
        self.reading_repository = SensorReadingRepository()
        self.thresholds_repository = StorageThresholdsRepository()
        self.actuator_event_repository = ActuatorEventRepository()
        self.reading_service = SensorReadingService()
        self.threshold_service = StorageThresholdService()
        self.condition_service = StorageConditionService()
        self.actuator_event_service = ActuatorEventService()

    def register_reading(
        self,
        device_id: str,
        temperature,
        humidity,
        recorded_at=None,
        api_key: str | None = None,
    ) -> tuple[SensorReading, str, str, bool, bool, ActuatorEvent | None]:
        if api_key is not None and not self.device_repository.find_by_id_and_api_key(device_id, api_key):
            raise ValueError("Device not found")

        next_sequence = self.reading_repository.next_sequence_for_device(device_id)

        reading = self.reading_service.create_reading(
            device_id,
            temperature,
            humidity,
            recorded_at,
            sequence=next_sequence,
        )
        saved_reading = self.reading_repository.save(reading)
        thresholds = self.get_current_thresholds(device_id)
        status = self.condition_service.evaluate(saved_reading, thresholds)
        actuator_command = self.condition_service.actuator_command(saved_reading, thresholds)
        humidity_alert, temperature_alert = self.condition_service.environmental_alerts(
            saved_reading, thresholds
        )
        actuator_event = None

        if actuator_command == ACTUATOR_ACTIVATE:
            actuator_event = self.actuator_event_repository.save(
                self.actuator_event_service.activate(device_id, saved_reading.recorded_at)
            )
        elif actuator_command == ACTUATOR_DEACTIVATE:
            actuator_event = self.actuator_event_repository.save(
                self.actuator_event_service.deactivate(device_id, saved_reading.recorded_at)
            )

        return saved_reading, status, actuator_command, humidity_alert, temperature_alert, actuator_event

    def get_current_thresholds(self, device_id: str) -> StorageThresholds:
        thresholds = self.thresholds_repository.find_current_by_device_id(device_id)
        if thresholds is not None:
            return thresholds

        default_thresholds = self.threshold_service.synchronize(
            device_id,
            DEFAULT_MIN_TEMPERATURE,
            DEFAULT_MAX_TEMPERATURE,
            DEFAULT_MIN_HUMIDITY,
            DEFAULT_MAX_HUMIDITY,
        )
        return self.thresholds_repository.save_current(default_thresholds)

    def update_thresholds(
        self,
        device_id: str,
        min_temperature,
        max_temperature,
        min_humidity,
        max_humidity,
        api_key: str | None = None,
    ) -> StorageThresholds:
        if api_key is not None and not self.device_repository.find_by_id_and_api_key(device_id, api_key):
            raise ValueError("Device not found")

        thresholds = self.threshold_service.synchronize(
            device_id,
            min_temperature,
            max_temperature,
            min_humidity,
            max_humidity,
        )
        return self.thresholds_repository.save_current(thresholds)

    def get_latest_reading(
        self, device_id: str
    ) -> tuple[SensorReading, str, str, bool, bool] | None:
        reading = self.reading_repository.find_latest_by_device_id(device_id)
        if reading is None:
            return None

        thresholds = self.get_current_thresholds(device_id)
        humidity_alert, temperature_alert = self.condition_service.environmental_alerts(reading, thresholds)
        return (
            reading,
            self.condition_service.evaluate(reading, thresholds),
            self.condition_service.actuator_command(reading, thresholds),
            humidity_alert,
            temperature_alert,
        )

    def get_recent_readings(
        self, device_id: str, limit: int = 10
    ) -> list[tuple[SensorReading, str, str, bool, bool]]:
        thresholds = self.get_current_thresholds(device_id)
        results = []
        for reading in self.reading_repository.find_recent_by_device_id(device_id, limit):
            humidity_alert, temperature_alert = self.condition_service.environmental_alerts(
                reading, thresholds
            )
            results.append(
                (
                    reading,
                    self.condition_service.evaluate(reading, thresholds),
                    self.condition_service.actuator_command(reading, thresholds),
                    humidity_alert,
                    temperature_alert,
                )
            )
        return results

    def get_sensor_status(self, device_id: str) -> dict:
        reading = self.reading_repository.find_latest_by_device_id(device_id)
        if reading is None:
            return {
                "device_id": device_id,
                "connection_status": "OFFLINE",
                "last_seen_at": None,
            }

        now = datetime.now(timezone.utc)
        recorded_at = reading.recorded_at
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now - recorded_at.astimezone(timezone.utc)).total_seconds()

        return {
            "device_id": device_id,
            "connection_status": "ONLINE" if elapsed_seconds <= 120 else "OFFLINE",
            "last_seen_at": reading.recorded_at,
        }

    def get_recent_actuator_events(self, device_id: str, limit: int = 10) -> list[ActuatorEvent]:
        return self.actuator_event_repository.find_recent_by_device_id(device_id, limit)
