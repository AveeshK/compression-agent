import os
import subprocess
import sys
import time
import zipfile

import pytest

from compression_agent.zipper import SevenZip, parse_skipped
from tests.conftest import SEVEN_ZIP, make_junction, requires_7z

pytestmark = requires_7z


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "Folder"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello")
    (src / "sub" / "b.txt").write_text("world")
    (src / "ünïcødé.txt").write_text("x")
    return tmp_path, src


def names(archive):
    with zipfile.ZipFile(archive) as z:
        return {n.rstrip("/") for n in z.namelist()}


def test_compress_success(tree):
    tmp, src = tree
    archive = str(tmp / "Folder.zip")
    seen = []
    r = SevenZip(SEVEN_ZIP).compress([str(src)], archive, timeout_s=60, on_progress=seen.append)
    assert r.ok, r.error
    assert r.exit_code == 0 and r.skipped_count == 0
    assert r.size_bytes == os.path.getsize(archive)
    assert not os.path.exists(archive + ".partial")
    assert {"Folder/a.txt", "Folder/sub/b.txt", "Folder/ünïcødé.txt"} <= names(archive)


def test_junction_is_not_followed(tree):
    tmp, src = tree
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    make_junction(src / "link", outside)

    archive = str(tmp / "Folder.zip")
    r = SevenZip(SEVEN_ZIP).compress([str(src)], archive, timeout_s=60)
    assert r.ok, r.error
    assert not any("secret" in n for n in names(archive))


def test_locked_file_is_reported_not_fatal(tree):
    tmp, src = tree
    locked = src / "locked.txt"
    locked.write_text("x")
    # Hold an exclusive (no-share) handle via PowerShell for the duration.
    holder = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"$f=[IO.File]::Open('{locked}','Open','ReadWrite','None'); 'ready'; Start-Sleep 30"],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout.readline().strip() == "ready"
        archive = str(tmp / "Folder.zip")
        r = SevenZip(SEVEN_ZIP).compress([str(src)], archive, timeout_s=60)
    finally:
        holder.kill()
    assert r.ok and r.exit_code == 1
    assert r.skipped_count == 1 and "locked.txt" in r.skipped_files[0]
    assert "Folder/a.txt" in names(archive)


def test_existing_partial_is_replaced(tree):
    tmp, src = tree
    archive = str(tmp / "Folder.zip")
    with open(archive + ".partial", "wb") as f:
        f.write(b"garbage")
    r = SevenZip(SEVEN_ZIP).compress([str(src)], archive, timeout_s=60)
    assert r.ok, r.error
    assert "Folder/a.txt" in names(archive)


@pytest.fixture
def big_tree(tmp_path):
    src = tmp_path / "Big"
    src.mkdir()
    for i in range(4):
        (src / f"blob{i}.bin").write_bytes(os.urandom(64 * 1024 * 1024))
    return tmp_path, src


def test_cancel_kills_and_cleans_up(big_tree):
    tmp, src = big_tree
    archive = str(tmp / "Big.zip")
    r = SevenZip(SEVEN_ZIP).compress(
        [str(src)], archive, timeout_s=600, should_cancel=lambda: True, poll_interval_s=0.01
    )
    assert not r.ok and r.error == "cancelled"
    assert not os.path.exists(archive) and not os.path.exists(archive + ".partial")


def test_timeout(big_tree):
    tmp, src = big_tree
    archive = str(tmp / "Big.zip")
    r = SevenZip(SEVEN_ZIP).compress([str(src)], archive, timeout_s=0, poll_interval_s=0.01)
    assert not r.ok and r.error.startswith("timed out")
    assert not os.path.exists(archive + ".partial")


def _7z_descendants(pid: int) -> list[int]:
    # Walk the whole tree: a venv's python.exe is a launcher that runs the real
    # interpreter as a child, so 7z is a grandchild of the process we started.
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | ForEach-Object "
         "{ \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"],
        capture_output=True, text=True,
    ).stdout  # fmt: skip
    procs = [line.split(" ", 2) for line in out.splitlines() if line.count(" ") >= 2]
    tree, found, frontier = {}, [], [pid]
    for p, ppid, name in procs:
        tree.setdefault(int(ppid), []).append((int(p), name))
    while frontier:
        for child, name in tree.get(frontier.pop(), []):
            frontier.append(child)
            if name.lower() == "7z.exe":
                found.append(child)
    return found


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout  # fmt: skip
    return str(pid) in out


def test_7z_dies_with_its_parent(big_tree):
    tmp, src = big_tree
    archive = tmp / "Big.zip"
    script = (
        "import sys; from compression_agent.zipper import SevenZip; "
        f"SevenZip(r'{SEVEN_ZIP}', 9).compress([r'{src}'], r'{archive}', timeout_s=600)"
    )
    parent = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 15
        while not (kids := _7z_descendants(parent.pid)) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert kids, "7z never started"
    finally:
        parent.kill()  # hard kill, like a crashed or stopped service
        parent.wait()
    time.sleep(1)
    assert not any(_alive(k) for k in kids)


def test_missing_source_fails(tmp_path):
    archive = str(tmp_path / "x.zip")
    r = SevenZip(SEVEN_ZIP).compress([str(tmp_path / "nope")], archive, timeout_s=60)
    assert not r.ok and r.error
    assert not os.path.exists(archive) and not os.path.exists(archive + ".partial")


def test_parse_skipped():
    lines = [
        "Archive size: 1 KiB",
        "WARNINGS for files:",
        r"\\srv\s\a.txt : locked",
        r"\\srv\s\b.txt : denied",
        "----------------",
        "WARNING: Cannot open 2 files",
    ]
    assert parse_skipped(lines) == [r"\\srv\s\a.txt : locked", r"\\srv\s\b.txt : denied"]
