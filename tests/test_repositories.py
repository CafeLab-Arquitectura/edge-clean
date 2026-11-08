from datetime import datetime, timedelta, timezone
import logging

from iotmonitoring.domain.entities import SensorReading
from iotmonitoring.infrastructure.models import SensorReadingModel
from iotmonitoring.infrastructure.repositories import SensorReadingRepository


def test_incremental_sequence_per_device():
    dev1 = "sensor-esp32-01"
    dev2 = "sensor-esp32-02"

    assert SensorReadingRepository.next_sequence_for_device(dev1) == 1
    assert SensorReadingRepository.next_sequence_for_device(dev2) == 1

    r1 = SensorReadingRepository.save(
        SensorReading(dev1, 22.0, 50.0, datetime.now(timezone.utc))
    )
    assert r1.sequence == 1

    r2 = SensorReadingRepository.save(
        SensorReading(dev1, 22.5, 51.0, datetime.now(timezone.utc))
    )
    assert r2.sequence == 2

    # dev2 has independent sequence
    r3 = SensorReadingRepository.save(
        SensorReading(dev2, 23.0, 55.0, datetime.now(timezone.utc))
    )
    assert r3.sequence == 1

    r4 = SensorReadingRepository.save(
        SensorReading(dev1, 22.8, 52.0, datetime.now(timezone.utc))
    )
    assert r4.sequence == 3


def test_purge_expired_unsynced_older_than_7_days(caplog):
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=8)
    recent_time = now - timedelta(days=2)

    # 1 expired unsynced reading
    SensorReadingModel.create(
        device_id="sensor-01",
        temperature=20.0,
        humidity=60.0,
        recorded_at=old_time,
        sequence=1,
        is_synced=False,
    )
    # 1 expired synced reading (should NOT be purged by unsynced purge)
    SensorReadingModel.create(
        device_id="sensor-01",
        temperature=21.0,
        humidity=61.0,
        recorded_at=old_time,
        sequence=2,
        is_synced=True,
    )
    # 1 recent unsynced reading (should NOT be purged)
    SensorReadingModel.create(
        device_id="sensor-01",
        temperature=22.0,
        humidity=62.0,
        recorded_at=recent_time,
        sequence=3,
        is_synced=False,
    )

    with caplog.at_level(logging.WARNING):
        deleted = SensorReadingRepository.purge_expired_unsynced(days=7)

    assert deleted == 1
    assert any("Descartadas" in rec.message and "7" in rec.message for rec in caplog.records)

    remaining = list(SensorReadingModel.select())
    assert len(remaining) == 2
    assert {r.sequence for r in remaining} == {2, 3}


def test_fifo_buffer_limit_discards_oldest_unsynced(caplog):
    now = datetime.now(timezone.utc)
    # Create 5 unsynced readings with increasing timestamps
    for i in range(1, 6):
        SensorReadingModel.create(
            device_id="sensor-01",
            temperature=20.0 + i,
            humidity=50.0 + i,
            recorded_at=now + timedelta(seconds=i),
            sequence=i,
            is_synced=False,
        )

    # Enforce buffer with max_records=3 (FIFO should discard sequences 1 and 2)
    with caplog.at_level(logging.WARNING):
        deleted = SensorReadingRepository.enforce_fifo_buffer_limit(max_records=3)

    assert deleted == 2
    assert any("desbordamiento" in rec.message for rec in caplog.records)

    remaining = list(SensorReadingModel.select().order_by(SensorReadingModel.sequence.asc()))
    assert len(remaining) == 3
    assert [r.sequence for r in remaining] == [3, 4, 5]

