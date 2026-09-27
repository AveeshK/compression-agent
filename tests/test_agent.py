import os
import zipfile
from datetime import timedelta

import pytest

from compression_agent.agent import RequestError, ZipAgent, choose_archive_path, summarize
from compression_agent.authz import AuthorizationError
from compression_agent.config import parse_settings
from compression_agent.store import JobMode, JobStatus, JobStore
from tests.conftest import SEVEN_ZIP, make_junction, requires_7z

USER = "alice@corp.com"


@pytest.fixture
def env(unc_tmp, tmp_path_factory):
    local, unc = unc_tmp
    share = local / "share"
    for name in ("Alpha", "Beta", "Gamma"):
        (share / name / "nested").mkdir(parents=True)
        (share / name / "file.txt").write_text(name)
        (share / name / "nested" / "deep.txt").write_text(name * 100)
    (share / "loose.txt").write_text("not a folder")
    outside = local / "outside"
    outside.mkdir()
    make_junction(share / "Sneaky", outside)

    db_dir = tmp_path_factory.mktemp("db")
    settings = parse_settings(
        {
            "seven_zip": str(SEVEN_ZIP),
            "db_path": str(db_dir / "jobs.sqlite3"),
            "roots": [{"path": unc + "\\share", "groups": ["CORP\\Zippers"]}],
            "limits": {"min_free_gb": 0, "max_queued_per_user": 2},
            "auth": {"resolver": "static", "static": {USER: ["CORP\\Zippers"]}},
        }
    )
    return ZipAgent(settings), share, unc + "\\share"


def run(agent, job):
    claimed = agent.store.claim("test", job_id=job.id)
    assert claimed is not None
    return agent.run_job(claimed)


def test_scan_lists_real_folders_only(env):
    agent, _, root = env
    scan = agent.scan(USER, root)
    assert [f.name for f in scan.folders] == ["Alpha", "Beta", "Gamma"]  # no file, no junction


@pytest.mark.parametrize(
    "path_suffix, exc, match",
    [
        ("\\..", RequestError, "relative"),
        ("\\Sneaky", RequestError, "junction"),
        ("\\Missing", RequestError, "does not exist"),
    ],
)
def test_scan_rejects(env, path_suffix, exc, match):
    agent, _, root = env
    with pytest.raises(exc, match=match):
        agent.scan(USER, root + path_suffix)


def test_scan_outside_root_and_unauthorized(env):
    agent, _, root = env
    with pytest.raises(AuthorizationError):
        agent.scan(USER, root.rsplit("\\", 1)[0])  # parent of the root
    with pytest.raises(AuthorizationError):
        agent.scan("mallory@corp.com", root)


@pytest.mark.parametrize(
    "folders, match",
    [
        ([], "no folders"),
        (["Nope"], "not a subfolder"),
        (["Sneaky"], "not a subfolder"),
        (["..\\outside"], "plain folder name"),
        (["loose.txt"], "not a subfolder"),
    ],
)
def test_submit_rejects_bad_folders(env, folders, match):
    agent, _, root = env
    with pytest.raises(RequestError, match=match):
        agent.submit(USER, root, folders)


def test_submit_normalizes_names_and_limits(env):
    agent, _, root = env
    job = agent.submit(USER, root, ["alpha", "ALPHA"])
    assert job.folders == ["Alpha"] and job.mode is JobMode.PER_FOLDER
    with pytest.raises(RequestError, match="already being zipped"):
        agent.submit(USER, root, ["Alpha"])
    agent.submit(USER, root, ["Beta"])
    with pytest.raises(RequestError, match="2 jobs"):
        agent.submit(USER, root, ["Gamma"])


@requires_7z
def test_per_folder_job(env):
    agent, share, root = env
    job = agent.submit(USER, root, ["Alpha", "Beta"])
    done = run(agent, job)
    assert done.status is JobStatus.SUCCEEDED, summarize(done)
    assert [os.path.basename(r.archive) for r in done.results] == ["Alpha.zip", "Beta.zip"]
    with zipfile.ZipFile(share / "Alpha.zip") as z:
        assert "Alpha/nested/deep.txt" in z.namelist()
    assert "Alpha.zip" in summarize(done)


@requires_7z
def test_combined_job(env):
    agent, share, root = env
    done = run(agent, agent.submit(USER, root, ["Alpha", "Gamma"], combined_name="Both.zip"))
    assert done.status is JobStatus.SUCCEEDED, summarize(done)
    with zipfile.ZipFile(share / "Both.zip") as z:
        tops = {n.split("/")[0] for n in z.namelist()}
    assert tops == {"Alpha", "Gamma"}


@requires_7z
def test_existing_archive_gets_timestamp(env):
    agent, share, root = env
    (share / "Alpha.zip").write_bytes(b"keep me")
    done = run(agent, agent.submit(USER, root, ["Alpha"]))
    assert done.status is JobStatus.SUCCEEDED
    assert os.path.basename(done.results[0].archive).startswith("Alpha_")
    assert (share / "Alpha.zip").read_bytes() == b"keep me"


@requires_7z
def test_cancel_before_run(env):
    agent, _, root = env
    job = agent.submit(USER, root, ["Alpha"])
    assert agent.cancel(USER, job.id).status is JobStatus.CANCELLED
    assert agent.store.claim("test", job_id=job.id) is None
    with pytest.raises(RequestError):
        agent.cancel("bob@corp.com", job.id)


def test_choose_archive_path(tmp_path):
    d = str(tmp_path)
    assert choose_archive_path(d, "X", "fail").endswith("\\X.zip")
    (tmp_path / "X.zip").write_bytes(b"")
    with pytest.raises(FileExistsError):
        choose_archive_path(d, "X", "fail")
    assert choose_archive_path(d, "X", "overwrite").endswith("\\X.zip")
    assert "\\X_" in choose_archive_path(d, "X", "timestamp")


def test_store_claim_is_exclusive_and_stale_jobs_fail(tmp_path):
    store = JobStore(tmp_path / "s.db")
    a = store.add(USER, r"\\s\x", ["A"], JobMode.PER_FOLDER)
    b = store.add(USER, r"\\s\x", ["B"], JobMode.PER_FOLDER)
    assert store.claim("w1").id == a.id
    assert store.claim("w2").id == b.id
    assert store.claim("w3") is None
    assert store.fail_stale(timedelta(minutes=5)) == []
    stale = store.fail_stale(timedelta(seconds=-1))
    assert {j.id for j in stale} == {a.id, b.id}
    assert all(j.status is JobStatus.FAILED for j in stale)
