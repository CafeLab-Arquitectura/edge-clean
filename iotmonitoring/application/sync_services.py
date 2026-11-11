import logging

from iam.infrastructure.repositories import DeviceRepository
from iotmonitoring.domain.entities import StorageThresholds
from iotmonitoring.domain.services import StorageThresholdService
from iotmonitoring.infrastructure.backend_client import (
    BackendClient,
    BackendRejectedError,
)
from iotmonitoring.infrastructure.mqtt_client import MqttTelemetryPublisher
from iotmonitoring.infrastructure.repositories import (
    SensorReadingRepository,
    StorageThresholdsRepository,
)
from shared.infrastructure.config import BackendConfig, MqttConfig

logger = logging.getLogger(__name__)


class TelemetrySyncService:
    """Orchestrates edge synchronization through HTTP or MQTT.

    SYNC_TRANSPORT=http preserves the existing direct API Gateway flow.
    SYNC_TRANSPORT=mqtt publishes telemetry to Mosquitto; mqtt-bridge then
    forwards it to the API Gateway.
    """

    MIN_INTERVAL_SECONDS = 5

    def __init__(
        self,
        backend_client: BackendClient | None = None,
        mqtt_publisher: MqttTelemetryPublisher | None = None,
        config: BackendConfig | None = None,
    ):
        self.config = config or BackendConfig.resolve()
        self.backend_client = backend_client or BackendClient(self.config)
        self.mqtt_publisher = mqtt_publisher or MqttTelemetryPublisher(MqttConfig.from_env())
        self.device_repository = DeviceRepository()
        self.reading_repository = SensorReadingRepository()
        self.thresholds_repository = StorageThresholdsRepository()
        self.threshold_service = StorageThresholdService()
        self.last_interval_seconds: int | None = None

    def _coffee_lot_id_for(self, device_id: str) -> int | None:
        device = self.device_repository.find_by_id(device_id)
        if device is None:
            logger.warning(
                "Device %s is not registered in onboarding; cannot map to coffeeLotId",
                device_id,
            )
            return None

        raw_lot_id = device.lot_id
        if raw_lot_id is None or str(raw_lot_id).strip().lower() in {"", "null", "none"}:
            logger.warning(
                "Device %s has no lot assigned in onboarding; skipping telemetry sync",
                device_id,
            )
            return None

        try:
            return int(str(raw_lot_id).strip())
        except (TypeError, ValueError):
            logger.warning(
                "Device %s has a non-numeric lot_id (%r); cannot map to coffeeLotId",
                device_id,
                raw_lot_id,
            )
            return None

    def push_pending_readings(self, batch_size: int = 50) -> dict:
        """Drain unsynced readings through HTTP or MQTT."""
        readings = self.reading_repository.find_unsynced(batch_size)
        pushed = 0
        skipped = 0
        transport = self.config.sync_transport

        for reading in readings:
            coffee_lot_id = self._coffee_lot_id_for(reading.device_id)
            if coffee_lot_id is None:
                skipped += 1
                continue

            try:
                if transport == "mqtt":
                    self.mqtt_publisher.publish_telemetry(
                        coffee_lot_id,
                        reading.temperature,
                        reading.humidity,
                        reading.recorded_at,
                        device_id=reading.device_id,
                        sequence=reading.sequence,
                    )
                else:
                    self.backend_client.post_telemetry(
                        coffee_lot_id,
                        reading.temperature,
                        reading.humidity,
                        reading.recorded_at,
                        device_id=reading.device_id,
                        sequence=reading.sequence,
                    )
                    try:
                        self.mqtt_publisher.publish_telemetry(
                            coffee_lot_id,
                            reading.temperature,
                            reading.humidity,
                            reading.recorded_at,
                            device_id=reading.device_id,
                            sequence=reading.sequence,
                        )
                    except Exception as mqtt_err:
                        logger.debug("MQTT mirror skipped: %s", mqtt_err)
            except BackendRejectedError as error:
                logger.warning("Dropping reading %s (rejected): %s", reading.id, error)
                self.reading_repository.mark_synced(reading.id)
                skipped += 1
                continue

            self.reading_repository.mark_synced(reading.id)
            pushed += 1

        return {
            "pushed": pushed,
            "skipped": skipped,
            "examined": len(readings),
            "transport": transport,
        }

    def pull_thresholds(self, device_id: str) -> StorageThresholds | None:
        """Pull the authoritative thresholds for a device and apply them locally."""
        coffee_lot_id = self._coffee_lot_id_for(device_id)
        if coffee_lot_id is None:
            return None

        payload = self.backend_client.get_thresholds(coffee_lot_id)
        if not payload:
            return None

        raw_interval = payload.get("syncIntervalSeconds")
        if raw_interval is not None:
            try:
                self.last_interval_seconds = max(
                    self.MIN_INTERVAL_SECONDS, int(raw_interval)
                )
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric syncIntervalSeconds: %r", raw_interval)

        try:
            thresholds = self.threshold_service.synchronize(
                device_id,
                payload["minTemperature"],
                payload["maxTemperature"],
                payload["minHumidity"],
                payload["maxHumidity"],
            )
        except (KeyError, ValueError) as error:
            logger.warning(
                "Backend thresholds for device %s are invalid for the edge: %s",
                device_id,
                error,
            )
            return None

        return self.thresholds_repository.save_current(thresholds)

    def pull_all_thresholds(self) -> int:
        if not (self.config.service_email and self.config.service_password):
            return 0

        updated = 0
        for device in self.device_repository.find_all():
            if device.lot_id is None:
                continue
            if self.pull_thresholds(device.device_id) is not None:
                updated += 1
        return updated


