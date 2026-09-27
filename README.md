# compression-agent

Zips folders on UNC shares using 7-Zip, running on a box close to the file
server (so the data never crosses the slow link to a user's machine). Users
pick a UNC path, choose subfolders, and get `<Folder>.zip` (or one combined
zip) written next to them in the same source folder.

Today the front end is a CLI; a Teams bot will call the same `ZipAgent` API.

## Setup

Requires Python 3.11+ and 7-Zip. No third-party runtime dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
copy config.example.toml config.toml   # then edit roots/groups
```

## Usage

```powershell
compression-agent roots                                  # roots you're allowed to use
compression-agent scan \\fileserver\projects\2026
compression-agent zip  \\fileserver\projects\2026 Alpha Beta            # Alpha.zip, Beta.zip
compression-agent zip  \\fileserver\projects\2026 --all --combined All  # All.zip
compression-agent zip  ... --no-wait                     # just queue it for the worker
compression-agent interactive                            # guided prompts (same flow as chat)
compression-agent worker                                 # process the queue
compression-agent jobs [--active] [--everyone] | status ID | cancel ID
```

`--user alice@corp.com` sets the requester (defaults to the current Windows
user's UPN). `--config` or `ZIPAGENT_CONFIG` points at the config file.

## How a job runs

1. **Validate**: the path is normalized (UNC only, no `..`, device paths, or
   odd characters), must sit under an allowed root, and the requester must be
   in one of that root's AD groups. Junctions/symlinks on the path are rejected.
2. **Queue**: the job goes into SQLite (`db_path`). A user may have at most
   `max_queued_per_user` active jobs, and a folder can't be in two active jobs.
3. **Run**: a worker claims it, re-validates, checks free space, and runs
   `7z a -tzip -mx=1 -mmt=on -snl -ssw ...` into `<name>.zip.partial`, renaming
   to `<name>.zip` only on success.
4. **Report**: locked/unreadable files don't fail the job. They're listed and
   the job ends `succeeded_with_warnings`.

Safety details worth knowing:

- `-snl` stops 7-Zip following junctions inside a folder. Without it, a
  junction to another share would be zipped in, bypassing the allowlist.
- 7-Zip runs in a Windows Job Object, so if the worker dies, 7-Zip dies too
  rather than continuing to write to the share.
- Running jobs heartbeat every 5 s (that's also how cancellation is picked up).
  Jobs with no heartbeat for 5 min are marked failed.
- If the target zip exists, `on_exists` decides: `timestamp` (default) writes
  `Folder_20260927-153000.zip`, `fail`, or `overwrite`.
- Every submit/start/finish/cancel goes to `log_path` as an audit trail.

## Deploying on the colo box

- Run `compression-agent worker` as a Windows service (NSSM or a Task Scheduler
  "at startup" task) under a **gMSA** that can read the allowed roots and
  write into them, and nothing else.
- The box must be domain-joined for `auth.resolver = "windows"` (it resolves
  transitive group membership via S4U; no extra privileges needed).
- Keep `max_concurrent_jobs` low (2): each 7-Zip already uses every core, and
  the file server's IOPS is usually the real bottleneck with many small files.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```

End-to-end tests use `\\localhost\C$` to exercise real UNC paths (skipped if
the admin share isn't reachable).

## Next: Teams

`ZipAgent` (`scan`, `submit`, `cancel`, `store.get`) is the whole API a bot
needs; `summarize(job)` renders the result message. Hooks already in place:

- `Job.reply_to` stores an opaque dict (e.g. the Teams conversation reference)
  so the worker can reply proactively when the job finishes.
- `WorkerPool(on_finished=...)` is where that notifier plugs in.

For a queue-based split (no inbound HTTPS to the colo box): a bot hosted in
Azure puts `{user, path, folders, combined_name, reply_to}` on a Service Bus
queue; a small consumer on the colo box calls `agent.submit(...)`, so all
validation and authorization still happen on the colo box. Results flow back
via a second queue (or the bot's proactive-messaging endpoint).
