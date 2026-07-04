import os


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _sync_transport_from_env() -> str:
    value = os.environ.get("SYNC_TRANSPORT", "http").strip().lower()
    return value if value in {"http", "mqtt"} else "http"


class MqttConfig:
    """Configuration for the local MQTT telemetry broker."""

    def __init__(
        self,
        host: str,
        port: int,
        topic: str,
        client_id: str,
        qos: int = 1,
        publish_timeout_seconds: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.topic = topic
        self.client_id = client_id
        self.qos = qos
        self.publish_timeout_seconds = publish_timeout_seconds

    @classmethod
    def from_env(cls) -> "MqttConfig":
        return cls(
            host=os.environ.get("MQTT_BROKER_HOST", "localhost"),
            port=int(os.environ.get("MQTT_BROKER_PORT", "1883")),
            topic=os.environ.get("MQTT_TELEMETRY_TOPIC", "cafelab/iot/telemetry"),
            client_id=os.environ.get("MQTT_CLIENT_ID", "cafelab-edge-local"),
            qos=int(os.environ.get("MQTT_QOS", "1")),
            publish_timeout_seconds=float(os.environ.get("MQTT_PUBLISH_TIMEOUT_SECONDS", "5")),
        )


class BackendConfig:
    """Configuration for the outbound connection to CafeLab.

    SYNC_TRANSPORT=http keeps the current direct HTTP flow.
    SYNC_TRANSPORT=mqtt publishes telemetry to Mosquitto and lets mqtt-bridge
    forward it to the API Gateway.
    """

    def __init__(
        self,
        base_url: str,
        service_email: str | None,
        service_password: str | None,
        timeout_seconds: float = 10.0,
        sync_enabled: bool = True,
        sync_interval_seconds: int = 10,
        sync_transport: str = "http",
    ):
        self.base_url = base_url
        self.service_email = service_email
        self.service_password = service_password
        self.timeout_seconds = timeout_seconds
        self.sync_interval_seconds = sync_interval_seconds
        self.sync_transport = sync_transport if sync_transport in {"http", "mqtt"} else "http"
        # HTTP sync needs backend credentials. MQTT telemetry can run without
        # credentials because mqtt-bridge owns the gateway forwarding step.
        self.sync_enabled = sync_enabled and (
            self.sync_transport == "mqtt" or bool(service_email and service_password)
        )

    @classmethod
    def from_env(cls) -> "BackendConfig":
        return cls(
            base_url=os.environ.get("BACKEND_BASE_URL", "http://localhost:8080"),
            service_email=os.environ.get("BACKEND_SERVICE_EMAIL"),
            service_password=os.environ.get("BACKEND_SERVICE_PASSWORD"),
            timeout_seconds=float(os.environ.get("BACKEND_TIMEOUT_SECONDS", "10")),
            sync_enabled=_as_bool(os.environ.get("BACKEND_SYNC_ENABLED"), default=True),
            sync_interval_seconds=int(os.environ.get("BACKEND_SYNC_INTERVAL_SECONDS", "10")),
            sync_transport=_sync_transport_from_env(),
        )

    @classmethod
    def resolve(cls) -> "BackendConfig":
        """Prefer the account onboarded via the edge UI; fall back to env vars."""
        account = None
        try:
            from iotmonitoring.infrastructure.repositories import BackendAccountRepository

            account = BackendAccountRepository.get()
        except Exception:
            account = None

        if account is None:
            return cls.from_env()

        return cls(
            base_url=account.base_url,
            service_email=account.email,
            service_password=account.password,
            timeout_seconds=float(os.environ.get("BACKEND_TIMEOUT_SECONDS", "10")),
            sync_enabled=_as_bool(os.environ.get("BACKEND_SYNC_ENABLED"), default=True),
            sync_interval_seconds=int(os.environ.get("BACKEND_SYNC_INTERVAL_SECONDS", "10")),
            sync_transport=_sync_transport_from_env(),
        )
