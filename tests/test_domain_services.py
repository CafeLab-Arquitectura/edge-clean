from datetime import datetime, timezone

from iotmonitoring.domain.entities import SensorReading, StorageThresholds
from iotmonitoring.domain.services import (
    ACTUATOR_ACTIVATE,
    ACTUATOR_DEACTIVATE,
    STATUS_DANGER,
    STATUS_OPTIMAL,
    STATUS_WARNING,
    SensorReadingService,
    StorageConditionService,
)


def _make_thresholds(
    min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0
) -> StorageThresholds:
    return StorageThresholds("sensor-01", min_temp, max_temp, min_hum, max_hum)


def _make_reading(temperature: float, humidity: float) -> SensorReading:
    return SensorReading("sensor-01", temperature, humidity, datetime.now(timezone.utc), sequence=1)


def test_actuator_command_activate_when_humidity_above_max():
    thresholds = _make_thresholds(max_hum=70.0)
    reading = _make_reading(20.0, 75.0)
    assert StorageConditionService.actuator_command(reading, thresholds) == ACTUATOR_ACTIVATE


def test_actuator_command_deactivate_when_humidity_below_or_equal_max():
    thresholds = _make_thresholds(max_hum=70.0)
    reading_equal = _make_reading(20.0, 70.0)
    reading_below = _make_reading(20.0, 65.0)
    assert StorageConditionService.actuator_command(reading_equal, thresholds) == ACTUATOR_DEACTIVATE
    assert StorageConditionService.actuator_command(reading_below, thresholds) == ACTUATOR_DEACTIVATE


def test_evaluate_status_warning_when_humidity_below_min():
    thresholds = _make_thresholds(min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0)
    reading = _make_reading(20.0, 45.0)
    assert StorageConditionService.evaluate(reading, thresholds) == STATUS_WARNING


def test_evaluate_status_danger_when_humidity_above_max():
    thresholds = _make_thresholds(min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0)
    reading = _make_reading(20.0, 75.0)
    assert StorageConditionService.evaluate(reading, thresholds) == STATUS_DANGER


def test_evaluate_status_danger_when_temperature_out_of_range():
    thresholds = _make_thresholds(min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0)
    reading_hot = _make_reading(30.0, 60.0)
    reading_cold = _make_reading(10.0, 60.0)
    assert StorageConditionService.evaluate(reading_hot, thresholds) == STATUS_DANGER
    assert StorageConditionService.evaluate(reading_cold, thresholds) == STATUS_DANGER


def test_evaluate_status_optimal_when_all_in_range():
    thresholds = _make_thresholds(min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0)
    reading = _make_reading(20.0, 60.0)
    assert StorageConditionService.evaluate(reading, thresholds) == STATUS_OPTIMAL


def test_environmental_alerts_humidity_alert_only_when_above_max():
    thresholds = _make_thresholds(min_temp=15.0, max_temp=25.0, min_hum=50.0, max_hum=70.0)

    # Below min humidity: humidity_alert must be FALSE
    reading_dry = _make_reading(20.0, 45.0)
    hum_alert, temp_alert = StorageConditionService.environmental_alerts(reading_dry, thresholds)
    assert hum_alert is False
    assert temp_alert is False

    # Above max humidity: humidity_alert must be TRUE
    reading_humid = _make_reading(20.0, 75.0)
    hum_alert, temp_alert = StorageConditionService.environmental_alerts(reading_humid, thresholds)
    assert hum_alert is True
    assert temp_alert is False

    # In range: both false
    reading_ok = _make_reading(20.0, 60.0)
    hum_alert, temp_alert = StorageConditionService.environmental_alerts(reading_ok, thresholds)
    assert hum_alert is False
    assert temp_alert is False

