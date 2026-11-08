from collections import namedtuple
from datetime import datetime, timedelta, timezone
import logging

import peewee

from iotmonitoring.domain.entities import ActuatorEvent, SensorReading, StorageThresholds
from iotmonitoring.infrastructure.models import (
    ActuatorEventModel,
    BackendAccountModel,
    SensorReadingModel,
    StorageThresholdsModel,
)
from shared.infrastructure.database import DATABASE_PATH, db

logger = logging.getLogger(__name__)

BackendAccount = namedtuple("BackendAccount", ["base_url", "email", "password"])

MAX_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB buffer limit


class SensorReadingRepository:
    @staticmethod
    def get_max_sequence_by_device_id(device_id: str) -> int:
        max_seq = (
            SensorReadingModel
            .select(peewee.fn.MAX(SensorReadingModel.sequence))
            .where(SensorReadingModel.device_id == device_id)
            .scalar()
        )
        return int(max_seq) if max_seq is not None else 0

    @staticmethod
    def next_sequence_for_device(device_id: str) -> int:
        return SensorReadingRepository.get_max_sequence_by_device_id(device_id) + 1

    @staticmethod
    def purge_expired_unsynced(days: int = 7) -> int:
        """Descartar lecturas no sincronizadas con mas de 7 dias."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = (
            SensorReadingModel
            .delete()
            .where(
                (SensorReadingModel.is_synced == False)  # noqa: E712
                & (SensorReadingModel.recorded_at < cutoff)
            )
            .execute()
        )
        if deleted > 0:
            logging.warning(
                "Descartadas %d lecturas no sincronizadas con mas de %d dias (expiradas)",
                deleted,
                days,
            )
        return deleted

    @staticmethod
    def enforce_fifo_buffer_limit(
        max_bytes: int = MAX_BUFFER_BYTES,
        max_records: int | None = None,
    ) -> int:
        """Limitar el buffer local a max_bytes (50 MB) bajo politica FIFO."""
        deleted_total = 0
        while True:
            size_bytes = 0
            try:
                cursor = db.execute_sql("PRAGMA page_count")
                page_count = cursor.fetchone()[0]
                cursor = db.execute_sql("PRAGMA page_size")
                page_size = cursor.fetchone()[0]
                size_bytes = page_count * page_size
            except Exception:
                if DATABASE_PATH.exists():
                    size_bytes = DATABASE_PATH.stat().st_size

            oversize = size_bytes > max_bytes
            excess_records = 0
            if max_records is not None:
                unsynced_count = SensorReadingRepository.count_unsynced()
                if unsynced_count > max_records:
                    excess_records = unsynced_count - max_records

            if not oversize and excess_records == 0:
                break

            batch_limit = excess_records if (excess_records > 0 and not oversize) else min(100, excess_records if excess_records > 0 else 100)

            # Politica FIFO: descartar los registros no sincronizados mas antiguos
            oldest_batch = (
                SensorReadingModel
                .select(SensorReadingModel.id)
                .where(SensorReadingModel.is_synced == False)  # noqa: E712
                .order_by(SensorReadingModel.recorded_at.asc(), SensorReadingModel.id.asc())
                .limit(batch_limit)
            )
            oldest_ids = [r.id for r in oldest_batch]
            if not oldest_ids:
                break

            deleted = (
                SensorReadingModel
                .delete()
                .where(SensorReadingModel.id.in_(oldest_ids))
                .execute()
            )
            deleted_total += deleted

        if deleted_total > 0:
            logging.warning(
                "Descartadas %d lecturas por desbordamiento del buffer local bajo politica FIFO",
                deleted_total,
            )
        return deleted_total

    @staticmethod
    def save(reading: SensorReading) -> SensorReading:
        SensorReadingRepository.purge_expired_unsynced()
        SensorReadingRepository.enforce_fifo_buffer_limit()

        sequence = getattr(reading, "sequence", None)
        if sequence is None or sequence <= 0:
            sequence = SensorReadingRepository.next_sequence_for_device(reading.device_id)

        record = SensorReadingModel.create(
            device_id=reading.device_id,
            temperature=reading.temperature,
            humidity=reading.humidity,
            recorded_at=reading.recorded_at,
            sequence=sequence,
        )
        return SensorReading(
            record.device_id,
            record.temperature,
            record.humidity,
            record.recorded_at,
            record.id,
            record.sequence,
        )

    @staticmethod
    def find_latest_by_device_id(device_id: str) -> SensorReading | None:
        try:
            record = (
                SensorReadingModel
                .select()
                .where(SensorReadingModel.device_id == device_id)
                .order_by(SensorReadingModel.recorded_at.desc(), SensorReadingModel.id.desc())
                .get()
            )
            return SensorReading(
                record.device_id,
                record.temperature,
                record.humidity,
                record.recorded_at,
                record.id,
                getattr(record, "sequence", 1),
            )
        except peewee.DoesNotExist:
            return None

    @staticmethod
    def find_recent_by_device_id(device_id: str, limit: int = 10) -> list[SensorReading]:
        records = (
            SensorReadingModel
            .select()
            .where(SensorReadingModel.device_id == device_id)
            .order_by(SensorReadingModel.recorded_at.desc(), SensorReadingModel.id.desc())
            .limit(limit)
        )
        return [
            SensorReading(
                record.device_id,
                record.temperature,
                record.humidity,
                record.recorded_at,
                record.id,
                getattr(record, "sequence", 1),
            )
            for record in records
        ]

    @staticmethod
    def find_unsynced(limit: int = 50) -> list[SensorReading]:
        records = (
            SensorReadingModel
            .select()
            .where(SensorReadingModel.is_synced == False)  # noqa: E712 (peewee needs ==)
            .order_by(SensorReadingModel.recorded_at.asc(), SensorReadingModel.id.asc())
            .limit(limit)
        )
        return [
            SensorReading(
                record.device_id,
                record.temperature,
                record.humidity,
                record.recorded_at,
                record.id,
                getattr(record, "sequence", 1),
            )
            for record in records
        ]

    @staticmethod
    def count_unsynced() -> int:
        return (
            SensorReadingModel
            .select()
            .where(SensorReadingModel.is_synced == False)  # noqa: E712 (peewee needs ==)
            .count()
        )

    @staticmethod
    def count_by_device(device_id: str) -> int:
        return (
            SensorReadingModel
            .select()
            .where(SensorReadingModel.device_id == device_id)
            .count()
        )

    @staticmethod
    def delete_all() -> int:
        return SensorReadingModel.delete().execute()

    @staticmethod
    def mark_synced(reading_id: int, synced_at: datetime | None = None) -> None:
        (
            SensorReadingModel
            .update(is_synced=True, synced_at=synced_at or datetime.now(timezone.utc))
            .where(SensorReadingModel.id == reading_id)
            .execute()
        )


class StorageThresholdsRepository:
    @staticmethod
    def save_current(thresholds: StorageThresholds) -> StorageThresholds:
        (
            StorageThresholdsModel
            .update(is_current=False)
            .where(StorageThresholdsModel.device_id == thresholds.device_id)
            .execute()
        )
        record = StorageThresholdsModel.create(
            device_id=thresholds.device_id,
            min_temperature=thresholds.min_temperature,
            max_temperature=thresholds.max_temperature,
            min_humidity=thresholds.min_humidity,
            max_humidity=thresholds.max_humidity,
            is_current=True,
        )
        return StorageThresholds(
            record.device_id,
            record.min_temperature,
            record.max_temperature,
            record.min_humidity,
            record.max_humidity,
            record.id,
            record.is_current,
        )

    @staticmethod
    def delete_all() -> int:
        return StorageThresholdsModel.delete().execute()

    @staticmethod
    def find_current_by_device_id(device_id: str) -> StorageThresholds | None:
        try:
            record = (
                StorageThresholdsModel
                .select()
                .where(
                    (StorageThresholdsModel.device_id == device_id)
                    & (StorageThresholdsModel.is_current == True)
                )
                .order_by(StorageThresholdsModel.id.desc())
                .get()
            )
            return StorageThresholds(
                record.device_id,
                record.min_temperature,
                record.max_temperature,
                record.min_humidity,
                record.max_humidity,
                record.id,
                record.is_current,
            )
        except peewee.DoesNotExist:
            return None


class ActuatorEventRepository:
    @staticmethod
    def save(event: ActuatorEvent) -> ActuatorEvent:
        record = ActuatorEventModel.create(
            device_id=event.device_id,
            event_type=event.event_type,
            triggered_at=event.triggered_at,
            resolved_at=event.resolved_at,
        )
        return ActuatorEvent(
            record.device_id,
            record.event_type,
            record.triggered_at,
            record.id,
            record.resolved_at,
        )

    @staticmethod
    def delete_all() -> int:
        return ActuatorEventModel.delete().execute()

    @staticmethod
    def find_recent_by_device_id(device_id: str, limit: int = 10) -> list[ActuatorEvent]:
        records = (
            ActuatorEventModel
            .select()
            .where(ActuatorEventModel.device_id == device_id)
            .order_by(ActuatorEventModel.triggered_at.desc(), ActuatorEventModel.id.desc())
            .limit(limit)
        )
        return [
            ActuatorEvent(
                record.device_id,
                record.event_type,
                record.triggered_at,
                record.id,
                record.resolved_at,
            )
            for record in records
        ]


class BackendAccountRepository:
    @staticmethod
    def get() -> BackendAccount | None:
        try:
            record = (
                BackendAccountModel
                .select()
                .order_by(BackendAccountModel.id.desc())
                .get()
            )
            return BackendAccount(record.base_url, record.email, record.password)
        except peewee.DoesNotExist:
            return None

    @staticmethod
    def save(base_url: str, email: str, password: str) -> None:
        BackendAccountModel.delete().execute()
        BackendAccountModel.create(
            base_url=base_url,
            email=email,
            password=password,
            updated_at=datetime.now(timezone.utc),
        )
