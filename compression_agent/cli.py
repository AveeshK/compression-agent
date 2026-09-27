"""Command-line front end.

    compression-agent scan \\\\srv\\share\\Projects
    compression-agent zip  \\\\srv\\share\\Projects Alpha Beta
    compression-agent zip  \\\\srv\\share\\Projects --all --combined Projects-2026
    compression-agent interactive          # prompt-driven, mirrors the chat flow
    compression-agent worker               # process the queue (run as a service)
    compression-agent jobs | status ID | cancel ID | roots
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from compression_agent.agent import RequestError, WorkerPool, ZipAgent, summarize
from compression_agent.authz import AuthorizationError
from compression_agent.config import ConfigError, load_settings
from compression_agent.store import Job

log = logging.getLogger("compression_agent")


def current_user() -> str:
    try:
        upn = subprocess.run(
            ["whoami", "/upn"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if "@" in upn:
            return upn
    except OSError:
        pass
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")


def parse_selection(text: str, count: int) -> list[int]:
    """Parse "1,3,5-7" or "all" into sorted 0-based indexes."""
    text = text.strip().lower()
    if text in ("all", "*"):
        return list(range(count))
    picked: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        start, end = int(lo), int(hi or lo)
        if not (1 <= start <= end <= count):
            raise ValueError(f"{part} is out of range 1-{count}")
        picked.update(range(start - 1, end))
    if not picked:
        raise ValueError("nothing selected")
    return sorted(picked)


def run_inline(agent: ZipAgent, job: Job) -> Job:
    """Run a just-submitted job in this process, printing progress."""
    claimed = agent.store.claim(f"cli:{os.getpid()}", job_id=job.id)
    if claimed is None:  # a service worker grabbed it first
        print(f"Job {job.id} was picked up by a worker; use `status {job.id}` to follow it.")
        return job
    box: dict[str, Job] = {}
    t = threading.Thread(target=lambda: box.update(done=agent.run_job(claimed)), daemon=True)
    t.start()
    last = None
    try:
        while t.is_alive():
            t.join(2)
            current = agent.store.get(job.id)
            if current and current.progress and current.progress != last:
                last = current.progress
                print(f"  {last}", flush=True)
    except KeyboardInterrupt:
        print("\nCancelling...", flush=True)
        agent.store.request_cancel(job.id)
        t.join()
    return box["done"]


def cmd_roots(agent: ZipAgent, args) -> int:
    roots = agent.allowed_roots(args.user)
    if not roots:
        print(f"{args.user} is not permitted to use any root.")
        return 1
    print(f"Roots available to {args.user}:")
    for r in roots:
        print(f"  {r}")
    return 0


def cmd_scan(agent: ZipAgent, args) -> int:
    scan = agent.scan(args.user, args.path)
    print(f"{scan.path}  ({len(scan.folders)} subfolders)")
    for i, f in enumerate(scan.folders, 1):
        print(f"  {i:>3}. {f.name:<50} {f.modified:%Y-%m-%d %H:%M}")
    return 0


def cmd_zip(agent: ZipAgent, args) -> int:
    folders = args.folders
    if args.all:
        folders = [f.name for f in agent.scan(args.user, args.path).folders]
    job = agent.submit(args.user, args.path, folders, combined_name=args.combined)
    print(f"Queued job {job.id}: {', '.join(job.folders)}")
    if args.no_wait:
        return 0
    done = run_inline(agent, job)
    print(summarize(done))
    return 0 if done.status.startswith("succeeded") else 1


def cmd_interactive(agent: ZipAgent, args) -> int:
    roots = agent.allowed_roots(args.user)
    if not roots:
        print(f"{args.user} is not permitted to use any root.")
        return 1
    print("You can zip folders under:\n  " + "\n  ".join(roots))
    while True:
        raw = input("\nNetwork folder (UNC path): ").strip()
        try:
            scan = agent.scan(args.user, raw)
        except (RequestError, AuthorizationError) as e:
            print(f"  {e}")
            continue
        if not scan.folders:
            print("  No subfolders there.")
            continue
        break

    for i, f in enumerate(scan.folders, 1):
        print(f"  {i:>3}. {f.name:<50} {f.modified:%Y-%m-%d %H:%M}")
    while True:
        try:
            idx = parse_selection(input("Folders to zip (e.g. 1,3,5-7 or all): "), len(scan.folders))
            break
        except ValueError as e:
            print(f"  {e}")
    names = [scan.folders[i].name for i in idx]

    combined = None
    if len(names) > 1:
        mode = input("One zip per folder, or one combined zip? [P/c]: ").strip().lower()
        if mode.startswith("c"):
            combined = input("Combined archive name: ").strip()

    targets = [f"{combined}.zip"] if combined else [f"{n}.zip" for n in names]
    print(f"\nWill create in {scan.path}:\n  " + "\n  ".join(targets))
    if input("Proceed? [y/N]: ").strip().lower() != "y":
        print("Aborted.")
        return 1

    job = agent.submit(args.user, scan.path, names, combined_name=combined)
    print(f"Queued job {job.id}.")
    done = run_inline(agent, job)
    print(summarize(done))
    return 0 if done.status.startswith("succeeded") else 1


def cmd_worker(agent: ZipAgent, args) -> int:
    stop = threading.Event()
    pool = WorkerPool(agent, on_finished=lambda j: print(summarize(j), flush=True))
    print(f"Worker {pool.worker_id} running {pool.concurrency} slot(s). Ctrl+C to stop.")
    t = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(1)
    except KeyboardInterrupt:
        print("Stopping after current jobs finish (Ctrl+C again to abort)...")
        stop.set()
        t.join()
    return 0


def cmd_jobs(agent: ZipAgent, args) -> int:
    jobs = agent.store.list(
        requester=None if args.everyone else args.user, active_only=args.active, limit=args.limit
    )
    if not jobs:
        print("No jobs.")
    for j in jobs:
        progress = f" [{j.progress}]" if j.progress else ""
        print(f"{j.id:>5}  {j.status:<24} {j.created_at}  {j.requester}  "
              f"{j.source_dir} :: {', '.join(j.folders)}{progress}")  # fmt: skip
    return 0


def cmd_status(agent: ZipAgent, args) -> int:
    job = agent.store.get(args.id)
    if job is None:
        print(f"No job {args.id}")
        return 1
    print(summarize(job))
    return 0


def cmd_cancel(agent: ZipAgent, args) -> int:
    job = agent.cancel(args.user, args.id)
    print(f"Job {job.id}: {job.status}" + (" (cancel requested)" if job.cancel_requested else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="compression-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument("--config", default=os.environ.get("ZIPAGENT_CONFIG", "config.toml"))
    p.add_argument("--user", help="requester identity (UPN); defaults to the current user")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("roots", help="list roots you may zip under").set_defaults(fn=cmd_roots)

    s = sub.add_parser("scan", help="list subfolders of a UNC path")
    s.add_argument("path")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("zip", help="zip subfolders of a UNC path")
    s.add_argument("path")
    s.add_argument("folders", nargs="*", help="subfolder names")
    s.add_argument("--all", action="store_true", help="every subfolder")
    s.add_argument("--combined", metavar="NAME", help="one NAME.zip instead of one per folder")
    s.add_argument("--no-wait", action="store_true", help="queue for a worker and return")
    s.set_defaults(fn=cmd_zip)

    sub.add_parser("interactive", help="guided prompt flow").set_defaults(fn=cmd_interactive)
    sub.add_parser("worker", help="process queued jobs").set_defaults(fn=cmd_worker)

    s = sub.add_parser("jobs", help="list jobs")
    s.add_argument("--everyone", action="store_true")
    s.add_argument("--active", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("status", help="show one job")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("cancel", help="cancel a queued or running job")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_cancel)
    return p


def setup_logging(log_path: Path | None, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    log.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(fmt)
    log.addHandler(console)
    if log_path:
        file = logging.FileHandler(log_path, encoding="utf-8")
        file.setFormatter(fmt)
        log.addHandler(file)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    setup_logging(settings.log_path, args.verbose)
    args.user = args.user or current_user()
    agent = ZipAgent(settings)
    try:
        return args.fn(agent, args)
    except (RequestError, AuthorizationError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
