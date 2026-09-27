"""7-Zip wrapper: run one archive job with progress, timeout and cancellation."""

import codecs
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

# https://7-zip.opensource.jp/chm/cmdline/exit_codes.htm
EXIT_OK = 0
EXIT_WARNING = 1  # e.g. some files were locked and skipped
EXIT_MESSAGES = {
    2: "fatal error",
    7: "command line error",
    8: "not enough memory",
    255: "stopped by user",
}

_PROGRESS_RE = re.compile(r"^\s*(\d{1,3})%")
_SPLIT_RE = re.compile(r"[\r\n\b]+")
_MAX_SKIPPED_REPORTED = 200


@dataclass
class ZipResult:
    archive: str
    ok: bool
    exit_code: int | None = None
    duration_s: float = 0.0
    size_bytes: int | None = None
    skipped_count: int = 0
    skipped_files: list[str] = field(default_factory=list)  # capped sample
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ZipResult:
        return cls(**d)


def parse_skipped(lines: Sequence[str]) -> list[str]:
    """Extract "path : reason" entries from 7-Zip's "WARNINGS for files:" block."""
    skipped, in_block = [], False
    for line in lines:
        if line.startswith("WARNINGS for files:"):
            in_block = True
        elif in_block:
            if line.startswith("----"):
                break
            if line.strip():
                skipped.append(line.strip())
    return skipped


class SevenZip:
    def __init__(self, exe: str | Path, compression_level: int = 1):
        self.exe = str(exe)
        self.level = compression_level

    def check_installed(self) -> None:
        if not Path(self.exe).is_file():
            raise FileNotFoundError(f"7-Zip not found at {self.exe}")

    def build_command(self, archive: str, sources: Sequence[str]) -> list[str]:
        return [
            self.exe, "a",
            "-tzip",
            f"-mx={self.level}",
            "-mmt=on",     # use all cores
            "-snl",        # store junctions/symlinks as links; never follow them out of the tree
            "-ssw",        # also compress files open for writing by other processes
            "-spd",        # no wildcard expansion in source names
            "-sccUTF-8",   # UTF-8 console output so non-ASCII paths survive
            "-bsp1", "-bso1", "-bse1",  # progress, output and errors all on stdout
            "-y",
            "--",
            archive,
            *sources,
        ]  # fmt: skip

    def compress(
        self,
        sources: Sequence[str],
        archive: str,
        *,
        timeout_s: float,
        should_cancel: Callable[[], bool] = lambda: False,
        on_progress: Callable[[int], None] | None = None,
        poll_interval_s: float = 1.0,
    ) -> ZipResult:
        """Zip `sources` into `archive`.

        Writes to `<archive>.partial` and renames on success, so a finished
        name never refers to a half-written file. `archive` is replaced if it
        exists; choosing a free name is the caller's job.
        """
        # 7-Zip treats a missing source as a mere warning and writes an empty archive.
        if missing := [s for s in sources if not os.path.exists(s)]:
            return ZipResult(archive=archive, ok=False,
                             error="source not found: " + ", ".join(missing))  # fmt: skip

        partial = archive + ".partial"
        _remove_quietly(partial)  # 7z "a" would otherwise append to a stale one
        start = time.monotonic()

        proc = subprocess.Popen(
            self.build_command(partial, sources),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _tie_to_this_process(proc)
        lines: list[str] = []
        reader = threading.Thread(
            target=_read_output, args=(proc.stdout, lines, on_progress), daemon=True
        )
        reader.start()

        stop_reason = None
        while True:
            try:
                proc.wait(timeout=poll_interval_s)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - start > timeout_s:
                stop_reason = f"timed out after {timeout_s / 60:.0f} min"
            elif should_cancel():
                stop_reason = "cancelled"
            if stop_reason:
                proc.kill()
                proc.wait()
                break
        reader.join(timeout=10)
        duration = time.monotonic() - start

        result = ZipResult(archive=archive, ok=False, exit_code=proc.returncode,
                           duration_s=round(duration, 1))  # fmt: skip
        if stop_reason:
            _remove_quietly(partial)
            result.error = stop_reason
            return result

        if proc.returncode in (EXIT_OK, EXIT_WARNING):
            skipped = parse_skipped(lines)
            try:
                os.replace(partial, archive)
            except OSError as e:
                _remove_quietly(partial)
                result.error = f"archive created but could not be renamed into place: {e}"
                return result
            result.ok = True
            result.size_bytes = os.path.getsize(archive)
            result.skipped_count = len(skipped)
            result.skipped_files = skipped[:_MAX_SKIPPED_REPORTED]
            if proc.returncode == EXIT_WARNING and not skipped:
                result.skipped_files = [l for l in lines if l.startswith("WARNING")][-5:]
            return result

        _remove_quietly(partial)
        reason = EXIT_MESSAGES.get(proc.returncode, f"exit code {proc.returncode}")
        detail = [l for l in lines if "ERROR" in l.upper()] or lines[-5:]
        result.error = f"7-Zip {reason}: " + " | ".join(detail)[-1000:]
        return result


def _read_output(stream, lines: list[str], on_progress: Callable[[int], None] | None):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf, last_pct = "", -1
    while chunk := stream.read(65536):
        buf += decoder.decode(chunk)
        *tokens, buf = _SPLIT_RE.split(buf)
        for token in tokens:
            if m := _PROGRESS_RE.match(token):
                pct = int(m.group(1))
                if on_progress and pct != last_pct:
                    last_pct = pct
                    on_progress(pct)
            elif token.strip():
                lines.append(token.rstrip())
    if buf.strip() and not _PROGRESS_RE.match(buf):
        lines.append(buf.rstrip())


_kill_on_close_job = None
_job_lock = threading.Lock()


def _tie_to_this_process(proc: subprocess.Popen) -> None:
    """Make Windows kill `proc` when this process exits, even on a hard crash.

    Otherwise a restarted/killed worker leaves 7z.exe running as an orphan,
    still writing to the share.
    """
    global _kill_on_close_job
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )  # fmt: skip
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    with _job_lock:
        if _kill_on_close_job is None:

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]  # fmt: skip

            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            ):
                return
            _kill_on_close_job = handle  # intentionally never closed; the OS closes it on exit

    kernel32.AssignProcessToJobObject(_kill_on_close_job, wintypes.HANDLE(int(proc._handle)))


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
