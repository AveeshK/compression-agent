"""The front-end-agnostic API: scan, submit, cancel, and execute zip jobs.

Front ends (the CLI today, a Teams bot or queue consumer later) call ZipAgent;
workers call ZipAgent.run_job. Every user-supplied value is validated here.
"""

import logging
import os
import shutil
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from compression_agent.authz import Authorizer, make_resolver
from compression_agent.config import RootRule, Settings
from compression_agent.paths import PathError, check_no_reparse_points, join_child, normalize_unc
from compression_agent.scanner import FolderEntry, list_subfolders
from compression_agent.store import Job, JobMode, JobStatus, JobStore
from compression_agent.zipper import SevenZip, ZipResult

log = logging.getLogger("compression_agent")

HEARTBEAT_INTERVAL_S = 5
STALE_AFTER = timedelta(minutes=5)


class RequestError(ValueError):
    """A request was invalid; the message is safe to show the requester."""


@dataclass(frozen=True)
class ScanResult:
    path: str
    root: RootRule
    folders: list[FolderEntry]


class ZipAgent:
    def __init__(
        self,
        settings: Settings,
        store: JobStore | None = None,
        authorizer: Authorizer | None = None,
        zipper: SevenZip | None = None,
    ):
        self.settings = settings
        self.store = store or JobStore(settings.db_path)
        self.authorizer = authorizer or Authorizer(settings.roots, make_resolver(settings))
        self.zipper = zipper or SevenZip(settings.seven_zip, settings.compression_level)

    # ---- front-end API -------------------------------------------------

    def allowed_roots(self, user: str) -> list[str]:
        return [r.path for r in self.authorizer.allowed_roots(user)]

    def scan(self, user: str, raw_path: str) -> ScanResult:
        path, rule = self._resolve(user, raw_path)
        try:
            folders = list_subfolders(path)
        except OSError as e:
            raise RequestError(f"could not list {path}: {e.strerror or e}") from None
        return ScanResult(path, rule, folders)

    def submit(
        self,
        user: str,
        raw_path: str,
        folders: list[str],
        *,
        combined_name: str | None = None,
        reply_to: dict | None = None,
    ) -> Job:
        """Validate a request and queue it. Raises RequestError / AuthorizationError."""
        scan = self.scan(user, raw_path)
        if not folders:
            raise RequestError("no folders selected")

        # Map requested names to the real on-disk names (case-insensitive).
        on_disk = {f.name.lower(): f.name for f in scan.folders}
        selected: list[str] = []
        for name in folders:
            try:
                join_child(scan.path, name)
            except PathError as e:
                raise RequestError(str(e)) from None
            real = on_disk.get(name.lower())
            if real is None:
                raise RequestError(f"{name!r} is not a subfolder of {scan.path}")
            if real not in selected:
                selected.append(real)

        mode = JobMode.PER_FOLDER
        if combined_name is not None:
            mode = JobMode.COMBINED
            combined_name = combined_name.strip()
            if combined_name.lower().endswith(".zip"):
                combined_name = combined_name[:-4]
            try:
                join_child(scan.path, combined_name)
            except PathError as e:
                raise RequestError(f"bad archive name: {e}") from None

        if self.store.count_active(user) >= self.settings.max_queued_per_user:
            raise RequestError(
                f"you already have {self.settings.max_queued_per_user} jobs queued or running"
            )
        busy = {f.lower() for j in self.store.active_for_source(scan.path) for f in j.folders}
        if clash := [f for f in selected if f.lower() in busy]:
            raise RequestError(f"already being zipped by another job: {', '.join(clash)}")

        job = self.store.add(user, scan.path, selected, mode, combined_name, reply_to)
        log.info("job %s submitted by %s: %s %s %s", job.id, user, scan.path, mode, selected)
        return job

    def cancel(self, user: str, job_id: int) -> Job:
        job = self.store.get(job_id)
        if job is None or job.requester.lower() != user.lower():
            raise RequestError(f"no job {job_id} for {user}")
        if not job.status.is_active:
            raise RequestError(f"job {job_id} already {job.status}")
        log.info("job %s cancel requested by %s", job_id, user)
        return self.store.request_cancel(job_id)

    # ---- execution -----------------------------------------------------

    def run_job(self, job: Job) -> Job:
        """Execute a claimed (running) job to completion and record the outcome."""
        log.info("job %s started", job.id)
        results: list[ZipResult] = []
        try:
            path, rule = self._resolve(job.requester, job.source_dir)  # re-check at run time
            if job.mode is JobMode.COMBINED:
                targets = [(job.archive_name, [join_child(path, f) for f in job.folders])]
            else:
                targets = [(f, [join_child(path, f)]) for f in job.folders]

            cancelled = False
            for i, (stem, sources) in enumerate(targets, 1):
                label = f"{i}/{len(targets)} {stem}"
                if self.store.heartbeat(job.id, f"{label} starting"):
                    cancelled = True
                    break
                result = self._zip_one(job.id, path, rule, stem, sources, label)
                results.append(result)
                log.info("job %s %s: %s", job.id, label, result)
                if result.error == "cancelled":
                    cancelled = True
                    break

            if cancelled:
                status = JobStatus.CANCELLED
            elif any(not r.ok for r in results):
                status = JobStatus.FAILED
            elif any(r.skipped_count for r in results):
                status = JobStatus.SUCCEEDED_WITH_WARNINGS
            else:
                status = JobStatus.SUCCEEDED
            finished = self.store.finish(job.id, status, results)
        except (RequestError, PermissionError, PathError, OSError) as e:
            finished = self.store.finish(job.id, JobStatus.FAILED, results, str(e))
        except Exception as e:  # never leave a job stuck in "running"
            log.exception("job %s crashed", job.id)
            finished = self.store.finish(job.id, JobStatus.FAILED, results, f"internal error: {e}")
        log.info("job %s finished: %s", job.id, finished.status)
        return finished

    def _zip_one(
        self, job_id: int, path: str, rule: RootRule, stem: str, sources: list[str], label: str
    ) -> ZipResult:
        for src in sources:
            check_no_reparse_points(src, rule.path)

        free = shutil.disk_usage(path).free
        if free < self.settings.min_free_bytes:
            return ZipResult(
                archive=join_child(path, stem + ".zip"), ok=False,
                error=f"only {free / 1024**3:.1f} GB free on {path}; "
                      f"need at least {self.settings.min_free_bytes / 1024**3:.0f} GB",
            )  # fmt: skip

        try:
            archive = choose_archive_path(path, stem, self.settings.on_exists)
        except FileExistsError as e:
            return ZipResult(archive=str(e), ok=False, error=f"{e} already exists")

        # Heartbeat doubles as the cancel check; throttle it to one DB write per interval.
        state = {"pct": 0, "last": 0.0, "cancel": False}

        def tick() -> None:
            now = time.monotonic()
            if now - state["last"] >= HEARTBEAT_INTERVAL_S:
                state["last"] = now
                state["cancel"] = self.store.heartbeat(job_id, f"{label} {state['pct']}%")

        def on_progress(pct: int) -> None:
            state["pct"] = pct

        def should_cancel() -> bool:
            tick()
            return state["cancel"]

        return self.zipper.compress(
            sources,
            archive,
            timeout_s=self.settings.job_timeout_s,
            should_cancel=should_cancel,
            on_progress=on_progress,
        )

    def _resolve(self, user: str, raw_path: str) -> tuple[str, RootRule]:
        try:
            path = normalize_unc(raw_path)
        except PathError as e:
            raise RequestError(str(e)) from None
        rule = self.authorizer.authorize(user, path)
        try:
            check_no_reparse_points(path, rule.path)
        except PathError as e:
            raise RequestError(str(e)) from None
        if not os.path.isdir(path):
            raise RequestError(f"not a folder or not reachable: {path}")
        return path, rule


def choose_archive_path(directory: str, stem: str, on_exists: str) -> str:
    archive = join_child(directory, stem + ".zip")
    if not os.path.exists(archive) or on_exists == "overwrite":
        return archive
    if on_exists == "fail":
        raise FileExistsError(archive)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return join_child(directory, f"{stem}_{stamp}.zip")


class WorkerPool:
    """Runs up to N jobs concurrently from the store until stopped."""

    def __init__(
        self,
        agent: ZipAgent,
        on_finished: Callable[[Job], None] | None = None,
        concurrency: int | None = None,
        poll_interval_s: float = 3.0,
    ):
        self.agent = agent
        self.on_finished = on_finished
        self.concurrency = concurrency or agent.settings.max_concurrent_jobs
        self.poll_interval_s = poll_interval_s
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def run(self, stop: threading.Event) -> None:
        self.agent.zipper.check_installed()
        log.info("worker %s starting with %d slot(s)", self.worker_id, self.concurrency)
        threads = [
            threading.Thread(target=self._loop, args=(stop,), name=f"zip-worker-{i}", daemon=True)
            for i in range(self.concurrency)
        ]
        for t in threads:
            t.start()
        while not stop.wait(60):
            self._reap_stale()
        for t in threads:
            t.join()

    def _loop(self, stop: threading.Event) -> None:
        self._reap_stale()
        while not stop.is_set():
            job = self.agent.store.claim(self.worker_id)
            if job is None:
                stop.wait(self.poll_interval_s)
                continue
            self._notify(self.agent.run_job(job))

    def _reap_stale(self) -> None:
        for job in self.agent.store.fail_stale(STALE_AFTER):
            log.warning("job %s marked failed: worker stopped heartbeating", job.id)
            self._notify(job)

    def _notify(self, job: Job) -> None:
        if self.on_finished:
            try:
                self.on_finished(job)
            except Exception:
                log.exception("notifier failed for job %s", job.id)


def format_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def summarize(job: Job) -> str:
    """Human-readable outcome, suitable for a chat reply."""
    icon = {
        JobStatus.SUCCEEDED: "✅",
        JobStatus.SUCCEEDED_WITH_WARNINGS: "⚠️",
        JobStatus.FAILED: "❌",
        JobStatus.CANCELLED: "🚫",
    }.get(job.status, "⏳")
    lines = [f"{icon} Job {job.id} {job.status.replace('_', ' ')} — {job.source_dir}"]
    if job.status.is_active:
        lines[0] += f" ({job.progress})" if job.progress else ""
        lines.append("   Folders: " + ", ".join(job.folders))
    for r in job.results:
        if r.ok:
            line = f"   {r.archive}  {format_bytes(r.size_bytes)} in {format_duration(r.duration_s)}"
            if r.skipped_count:
                line += f"  ({r.skipped_count} file(s) skipped: locked or unreadable)"
        else:
            line = f"   {r.archive}  FAILED: {r.error}"
        lines.append(line)
        lines.extend(f"      - {s}" for s in r.skipped_files[:5])
        if r.skipped_count > 5:
            lines.append(f"      ... and {r.skipped_count - 5} more")
    if job.error:
        lines.append(f"   Error: {job.error}")
    return "\n".join(lines)
