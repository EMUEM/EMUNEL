"""Engine-layer persistence utilities (STABILIZATION stage 1).

Everything here is additive infrastructure for the engines layer:

  * BackupService  — rotating state backups inside DATA_DIR/backups
                     (cron every EMUNEL_BACKUP_INTERVAL_H, default 6h;
                     EMUNEL_BACKUP_KEEP copies, default 7)
  * ensure_wal()   — flip SQLite databases to WAL journal mode from the
                     outside (no console/core code touched; the setting
                     persists inside the file itself)

The service must NEVER take the host down: every operation is wrapped,
failures degrade to a logged skip and the next cron tick retries.
"""
from .backup import BackupService, ensure_wal

__all__ = ["BackupService", "ensure_wal"]
