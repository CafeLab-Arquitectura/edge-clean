from datetime import datetime, timezone
from unittest.mock import MagicMock

from iotmonitoring.application.services import IoTMonitoringApplicationService
from iotmonitoring.domain.services import ACTUATOR_ACTIVATE, ACTUATOR_DEACTIVATE
from iotmonitoring.infrastructure.backend_client import BackendClient
from iotmonitoring.infrastructure.models import ActuatorEventModel


def test_register_reading_persists_activate_event_on_high_humidity():
    app_service = IoTMonitoringApplicationService()
    device_id = "sensor-test-01"

    # Set thresholds: max humidity 70%
    app_service.update_thresholds(device_id, 15.0, 25.0, 50.0, 70.0)

    # Reading with humidity 75% > 70%
    (
        reading,
        status,
        actuator_command,
        humidity_alert,
        temperature_alert,
        actuator_event,
    ) = app_service.register_reading(device_id, 20.0, 75.0)

    assert reading.sequence == 1
    assert actuator_command == ACTUATOR_ACTIVATE
    assert humidity_alert is True
    assert actuator_event is not None
    assert actuator_event.event_type == ACTUATOR_ACTIVATE

    # Verify persisted in db
    persisted_events = list(ActuatorEventModel.select().where(ActuatorEventModel.device_id == device_id))
    assert len(persisted_events) == 1
    assert persisted_events[0].event_type == ACTUATOR_ACTIVATE


def test_register_reading_persists_deactivate_event_when_humidity_normal():
    app_service = IoTMonitoringApplicationService()
    device_id = "sensor-test-02"

    # Set thresholds: max humidity 70%
    app_service.update_thresholds(device_id, 15.0, 25.0, 50.0, 70.0)

    # Reading with humidity 65% <= 70%
    (
        reading,
        status,
        actuator_command,
        humidity_alert,
        temperature_alert,
        actuator_event,
    ) = app_service.register_reading(device_id, 20.0, 65.0)

    assert reading.sequence == 1
    assert actuator_command == ACTUATOR_DEACTIVATE
    assert humidity_alert is False
    assert actuator_event is not None
    assert actuator_event.event_type == ACTUATOR_DEACTIVATE

    # Verify persisted in db
    persisted_events = list(ActuatorEventModel.select().where(ActuatorEventModel.device_id == device_id))
    assert len(persisted_events) == 1
    assert persisted_events[0].event_type == ACTUATOR_DEACTIVATE


def test_backend_client_post_telemetry_includes_sequence_and_sensor_id():
    client = BackendClient()
    client._request = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 1}
    client._request.return_value = mock_response

    now = datetime(2026, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
    client.post_telemetry(
        coffee_lot_id=7,
        temperature=22.5,
        humidity=65.0,
        recorded_at=now,
        device_id="sensor-esp32-abc",
        sequence=42,
    )

    client._request.assert_called_once()
    call_args = client._request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1] == "/api/v1/telemetry-records"
    payload = call_args[1]["json"]
    assert payload["coffeeLotId"] == 7
    assert payload["temperature"] == 22.5
    assert payload["humidity"] == 65.0
    assert payload["sensorId"] == "sensor-esp32-abc"
    assert payload["sequence"] == 42
    assert "measuredAt" in payload
    assert "timestamp" in payload

