from datetime import timezone

from flask import Blueprint, jsonify, request

from iam.interfaces.services import authenticate_request
from iotmonitoring.application.services import IoTMonitoringApplicationService
from shared.infrastructure.sync_worker import worker as sync_worker

iotmonitoring_api = Blueprint("iotmonitoring_api", __name__)
iot_monitoring_service = IoTMonitoringApplicationService()

DEFAULT_DEVICE_ID = "tracksilo-001"


def _request_json() -> dict:
    return request.get_json(silent=True) or {}


def _get_device_id(data: dict | None = None) -> str:
    data = data or {}
    return (
        data.get("device_id")
        or data.get("deviceId")
        or request.args.get("device_id")
        or request.args.get("deviceId")
        or DEFAULT_DEVICE_ID
    )


def _get_limit() -> int:
    try:
        limit = int(request.args.get("limit", 10))
    except ValueError:
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def _format_datetime(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _thresholds_resource(thresholds) -> dict:
    return {
        "deviceId": thresholds.device_id,
        "minTemperature": thresholds.min_temperature,
        "maxTemperature": thresholds.max_temperature,
        "minHumidity": thresholds.min_humidity,
        "maxHumidity": thresholds.max_humidity,
    }


def _reading_resource(
    reading,
    status: str,
    actuator_command: str,
    humidity_alert: bool,
    temperature_alert: bool,
) -> dict:
    return {
        "readingId": reading.id,
        "deviceId": reading.device_id,
        "temperature": reading.temperature,
        "humidity": reading.humidity,
        "sequence": getattr(reading, "sequence", 1),
        "status": status,
        "actuatorCommand": actuator_command,
        "humidityAlert": humidity_alert,
        "temperatureAlert": temperature_alert,
        "recordedAt": _format_datetime(reading.recorded_at),
    }


def _actuator_event_resource(event) -> dict:
    return {
        "eventId": event.id,
        "deviceId": event.device_id,
        "eventType": event.event_type,
        "triggeredAt": _format_datetime(event.triggered_at),
        "resolvedAt": _format_datetime(event.resolved_at),
    }


@iotmonitoring_api.route("/api/v1/edge/thresholds", methods=["GET"])
def get_thresholds():
    device_id = _get_device_id()
    thresholds = iot_monitoring_service.get_current_thresholds(device_id)
    return jsonify(_thresholds_resource(thresholds)), 200


@iotmonitoring_api.route("/api/v1/edge/thresholds", methods=["PUT"])
def update_thresholds():
    auth_result = authenticate_request()
    if auth_result:
        return auth_result

    data = _request_json()
    device_id = _get_device_id(data)

    try:
        thresholds = iot_monitoring_service.update_thresholds(
            device_id,
            data["minTemperature"],
            data["maxTemperature"],
            data["minHumidity"],
            data["maxHumidity"],
            request.headers.get("X-API-Key"),
        )
        return jsonify(_thresholds_resource(thresholds)), 200
    except KeyError:
        return jsonify({"error": "Missing required threshold fields"}), 400
    except ValueError as error:
        status = 401 if str(error) == "Device not found" else 400
        return jsonify({"error": str(error)}), status


@iotmonitoring_api.route("/api/v1/edge/readings", methods=["POST"])
def register_reading():
    auth_result = authenticate_request()
    if auth_result:
        return auth_result

    data = _request_json()
    device_id = _get_device_id(data)

    try:
        (
            reading,
            status,
            actuator_command,
            humidity_alert,
            temperature_alert,
            _,
        ) = iot_monitoring_service.register_reading(
            device_id,
            data["temperature"],
            data["humidity"],
            data.get("recordedAt") or data.get("recorded_at") or data.get("measured_at") or data.get("measuredAt"),
            request.headers.get("X-API-Key"),
        )
        # Wake the sync worker so this reading is pushed now, not on the next poll.
        sync_worker.notify()
        return jsonify(
            _reading_resource(reading, status, actuator_command, humidity_alert, temperature_alert)
        ), 201
    except KeyError:
        return jsonify({"error": "Missing required reading fields"}), 400
    except ValueError as error:
        status = 401 if str(error) == "Device not found" else 400
        return jsonify({"error": str(error)}), status


@iotmonitoring_api.route("/api/v1/edge/readings/latest", methods=["GET"])
def get_latest_reading():
    device_id = _get_device_id()
    result = iot_monitoring_service.get_latest_reading(device_id)
    if result is None:
        return jsonify({"error": "No readings found for device"}), 404

    reading, status, actuator_command, humidity_alert, temperature_alert = result
    return jsonify(
        _reading_resource(reading, status, actuator_command, humidity_alert, temperature_alert)
    ), 200


@iotmonitoring_api.route("/api/v1/edge/readings", methods=["GET"])
def get_readings():
    device_id = _get_device_id()
    try:
        limit = _get_limit()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    readings = [
        _reading_resource(reading, status, actuator_command, humidity_alert, temperature_alert)
        for reading, status, actuator_command, humidity_alert, temperature_alert
        in iot_monitoring_service.get_recent_readings(device_id, limit)
    ]
    return jsonify({"readings": readings}), 200


@iotmonitoring_api.route("/api/v1/edge/sensor-status", methods=["GET"])
def get_sensor_status():
    sensor_status = iot_monitoring_service.get_sensor_status(_get_device_id())
    return jsonify({
        "deviceId": sensor_status["device_id"],
        "connectionStatus": sensor_status["connection_status"],
        "lastSeenAt": _format_datetime(sensor_status["last_seen_at"]),
    }), 200


@iotmonitoring_api.route("/api/v1/edge/sync", methods=["POST"])
def trigger_sync():
    auth_result = authenticate_request()
    if auth_result:
        return auth_result

    from iotmonitoring.application.sync_services import TelemetrySyncService

    service = TelemetrySyncService()
    try:
        push_result = service.push_pending_readings()
        thresholds_updated = service.pull_all_thresholds()
    except Exception as error:  # noqa: BLE001 - surface backend issues to caller
        return jsonify({"error": f"sync failed: {error}"}), 502

    from iotmonitoring.infrastructure.repositories import SensorReadingRepository

    return jsonify({
        "readingsPushed": push_result["pushed"],
        "readingsSkipped": push_result["skipped"],
        "thresholdsUpdated": thresholds_updated,
        "readingsPending": SensorReadingRepository.count_unsynced(),
    }), 200


@iotmonitoring_api.route("/api/v1/edge/sync/status", methods=["GET"])
def sync_status():
    from iotmonitoring.infrastructure.repositories import SensorReadingRepository

    return jsonify({
        "pendingReadings": SensorReadingRepository.count_unsynced(),
        "syncEnabled": sync_worker.config.sync_enabled,
        "workerRunning": sync_worker.is_running(),
        "intervalSeconds": sync_worker.config.sync_interval_seconds,
    }), 200


@iotmonitoring_api.route("/api/v1/edge/actuator-events", methods=["GET"])
def get_actuator_events():
    device_id = _get_device_id()
    try:
        limit = _get_limit()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    events = [
        _actuator_event_resource(event)
        for event in iot_monitoring_service.get_recent_actuator_events(device_id, limit)
    ]
    return jsonify({"events": events}), 200
