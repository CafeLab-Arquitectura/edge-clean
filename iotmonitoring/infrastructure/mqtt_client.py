import json
import logging
import threading
from datetime import datetime, timezone

from dateutil.parser import parse

from shared.infrastructure.config import MqttConfig

logger = logging.getLogger(__name__)


class MqttPublishError(Exception):
    """Raised when a telemetry message cannot be published to MQTT."""


def _to_mqtt_timestamp(value) -> str:
    """Render a timestamp as UTC ISO-8601 with milliseconds and trailing Z."""
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        dt = parse(str(value))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MqttTelemetryPublisher:
    """Publishes edge telemetry to the local CafeLab MQTT broker."""

    def __init__(self, config: MqttConfig | None = None):
        self.config = config or MqttConfig.from_env()
        self._client = None
        self._lock = threading.Lock()

    def _ensure_client(self):
        with self._lock:
            if self._client is not None:
                return self._client

            try:
                import paho.mqtt.client as mqtt
            except ImportError as error:
                raise MqttPublishError(
                    "paho-mqtt is not installed. Run: pip install -r requirements.txt"
                ) from error

            client = mqtt.Client(client_id=self.config.client_id, clean_session=True)
            logger.info(
                "Connecting MQTT publisher to %s:%s (clientId=%s)",
                self.config.host,
                self.config.port,
                self.config.client_id,
            )
            rc = client.connect(self.config.host, self.config.port, keepalive=60)
            if rc != 0:
                raise MqttPublishError(f"MQTT connect failed with rc={rc}")
            client.loop_start()
            self._client = client
            return client

    def publish_telemetry(self, coffee_lot_id: int, temperature, humidity, recorded_at) -> dict:
        payload = {
            "coffeeLotId": int(coffee_lot_id),
            "temperature": float(temperature),
            "humidity": float(humidity),
            "timestamp": _to_mqtt_timestamp(recorded_at),
        }
        message = json.dumps(payload, separators=(",", ":"))
        client = self._ensure_client()
        logger.info("Publishing telemetry to MQTT topic %s: %s", self.config.topic, payload)
        info = client.publish(self.config.topic, payload=message, qos=self.config.qos)
        info.wait_for_publish(timeout=self.config.publish_timeout_seconds)
        if not info.is_published():
            raise MqttPublishError(
                f"MQTT publish timeout after {self.config.publish_timeout_seconds}s"
            )
        return payload
