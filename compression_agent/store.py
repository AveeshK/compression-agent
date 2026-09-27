"""SQLite-backed job store shared by front ends (CLI, later Teams) and workers.

Safe to use from multiple threads and processes: each call opens its own
connection, the DB runs in WAL mode, and claiming a job is a single atomic
UPDATE ... RETURNING.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from compression_agent.zipper import ZipResult


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in (JobStatus.QUEUED, JobStatus.RUNNING)


class JobMode(StrEnum):
    PER_FOLDER = "per_folder"  # one <Folder>.zip per selected folder
    COMBINED = "combined"      # one <archive_name>.zip containing all selected folders


@dataclass
class Job:
    id: int
    requester: str
    source_dir: str
    folders: list[str]
    mode: JobMode
    status: JobStatus
    archive_name: str | None = None
    progress: str | None = None
    results: list[ZipResult] = field(default_factory=list)
    error: str | None = None
    reply_to: dict | None = None  # opaque to the core; e.g. a Teams conversation reference
    cancel_requested: bool = False
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    worker: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            requester=row["requester"],
            source_dir=row["source_dir"],
            folders=json.loads(row["folders"]),
            mode=JobMode(row["mode"]),
            status=JobStatus(row["status"]),
            archive_name=row["archive_name"],
            progress=row["progress"],
            results=[ZipResult.from_dict(r) for r in json.loads(row["results"] or "[]")],
            error=row["error"],
            reply_to=json.loads(row["reply_to"]) if row["reply_to"] else None,
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            heartbeat_at=row["heartbeat_at"],
            worker=row["worker"],
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    requester        TEXT NOT NULL,
    source_dir       TEXT NOT NULL,
    folders          TEXT NOT NULL,
    mode             TEXT NOT NULL,
    archive_name     TEXT,
    status           TEXT NOT NULL,
    progress         TEXT,
    results          TEXT,
    error            TEXT,
    reply_to         TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    heartbeat_at     TEXT,
    worker           TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id);
CREATE INDEX IF NOT EXISTS jobs_requester ON jobs (requester, status);
"""

_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def add(
        self,
        requester: str,
        source_dir: str,
        folders: list[str],
        mode: JobMode,
        archive_name: str | None = None,
        reply_to: dict | None = None,
    ) -> Job:
        with closing(self._connect()) as db:
            row = db.execute(
                """INSERT INTO jobs (requester, source_dir, folders, mode, archive_name,
                                     status, reply_to, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
                (
                    requester, source_dir, json.dumps(folders), mode.value, archive_name,
                    JobStatus.QUEUED.value, json.dumps(reply_to) if reply_to else None, _now(),
                ),
            ).fetchone()  # fmt: skip
        return Job.from_row(row)

    def get(self, job_id: int) -> Job | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list(
        self, requester: str | None = None, active_only: bool = False, limit: int = 50
    ) -> list[Job]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if requester:
            sql += " AND requester = ? COLLATE NOCASE"
            args.append(requester)
        if active_only:
            sql += " AND status IN (?, ?)"
            args.extend(_ACTIVE)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with closing(self._connect()) as db:
            return [Job.from_row(r) for r in db.execute(sql, args)]

    def active_for_source(self, source_dir: str) -> list[Job]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE source_dir = ? COLLATE NOCASE AND status IN (?, ?)",
                (source_dir, *_ACTIVE),
            ).fetchall()
        return [Job.from_row(r) for r in rows]

    def count_active(self, requester: str) -> int:
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT COUNT(*) FROM jobs WHERE requester = ? COLLATE NOCASE AND status IN (?, ?)",
                (requester, *_ACTIVE),
            ).fetchone()[0]

    def claim(self, worker: str, job_id: int | None = None) -> Job | None:
        """Atomically move the oldest (or a specific) queued job to running."""
        target = "?" if job_id is not None else (
            "(SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)"
        )
        args = [JobStatus.RUNNING.value, _now(), _now(), worker]
        if job_id is not None:
            args.append(job_id)
        with closing(self._connect()) as db:
            row = db.execute(
                f"""UPDATE jobs SET status = ?, started_at = ?, heartbeat_at = ?, worker = ?
                    WHERE id = {target} AND status = 'queued' RETURNING *""",
                args,
            ).fetchone()
        return Job.from_row(row) if row else None

    def heartbeat(self, job_id: int, progress: str | None = None) -> bool:
        """Record liveness/progress. Returns True if cancellation was requested."""
        with closing(self._connect()) as db:
            row = db.execute(
                """UPDATE jobs SET heartbeat_at = ?, progress = COALESCE(?, progress)
                   WHERE id = ? RETURNING cancel_requested""",
                (_now(), progress, job_id),
            ).fetchone()
        return bool(row and row[0])

    def finish(
        self,
        job_id: int,
        status: JobStatus,
        results: list[ZipResult],
        error: str | None = None,
    ) -> Job:
        with closing(self._connect()) as db:
            row = db.execute(
                """UPDATE jobs SET status = ?, results = ?, error = ?, finished_at = ?,
                                   progress = NULL
                   WHERE id = ? RETURNING *""",
                (status.value, json.dumps([r.to_dict() for r in results]), error, _now(), job_id),
            ).fetchone()
        return Job.from_row(row)

    def request_cancel(self, job_id: int) -> Job | None:
        """Cancel a queued job immediately; flag a running one for its worker."""
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ? AND status = 'queued'",
                (JobStatus.CANCELLED.value, _now(), job_id),
            )
            db.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'",
                (job_id,),
            )
        return self.get(job_id)

    def fail_stale(self, older_than: timedelta) -> list[Job]:
        """Fail running jobs whose worker stopped heartbeating (crash, reboot)."""
        cutoff = (datetime.now(timezone.utc) - older_than).isoformat(timespec="seconds")
        with closing(self._connect()) as db:
            rows = db.execute(
                """UPDATE jobs SET status = ?, finished_at = ?, progress = NULL,
                                   error = 'interrupted: worker stopped responding'
                   WHERE status = 'running' AND heartbeat_at < ? RETURNING *""",
                (JobStatus.FAILED.value, _now(), cutoff),
            ).fetchall()
        return [Job.from_row(r) for r in rows]
