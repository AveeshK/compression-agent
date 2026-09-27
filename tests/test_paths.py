import pytest

from compression_agent.paths import (
    PathError,
    check_no_reparse_points,
    is_within,
    join_child,
    normalize_unc,
)
from tests.conftest import make_junction


@pytest.mark.parametrize(
    "raw, expected",
    [
        (r"\\srv\share", r"\\srv\share"),
        (r"\\srv\share\a\b\\", r"\\srv\share\a\b"),
        ("//srv/share/a", r"\\srv\share\a"),
        (r'  "\\srv\share\with space"  ', r"\\srv\share\with space"),
        (r"\\srv\share\\a\\\b", r"\\srv\share\a\b"),
    ],
)
def test_normalize_ok(raw, expected):
    assert normalize_unc(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\data",
        r"relative\path",
        r"\\srv",
        r"\\srv\share\..\other",
        r"\\srv\share\a\.\b",
        r"\\?\UNC\srv\share",
        r"\\.\pipe\x",
        r"\\srv\share\a*b",
        r"\\srv\share\trailingdot.",
        r"\\srv\share\trailing \x",
        "\\\\srv\\share\\a\x00b",
    ],
)
def test_normalize_rejects(raw):
    with pytest.raises(PathError):
        normalize_unc(raw)


def test_is_within():
    root = r"\\srv\share\projects"
    assert is_within(r"\\srv\share\projects", root)
    assert is_within(r"\\SRV\Share\Projects\a", root)
    assert not is_within(r"\\srv\share\projects2", root)
    assert not is_within(r"\\srv\share", root)


@pytest.mark.parametrize("name", ["", "a\\b", "a/b", "..", ".", "x:y", "bad."])
def test_join_child_rejects(name):
    with pytest.raises(PathError):
        join_child(r"\\srv\share", name)


def test_join_child():
    assert join_child("\\\\srv\\share\\", "Folder") == r"\\srv\share\Folder"


def test_reparse_point_detected(tmp_path):
    root = tmp_path / "root"
    (root / "real" / "deep").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(root / "link", outside)

    check_no_reparse_points(str(root / "real" / "deep"), str(root))
    with pytest.raises(PathError, match="junction"):
        check_no_reparse_points(str(root / "link"), str(root))
    with pytest.raises(PathError, match="does not exist"):
        check_no_reparse_points(str(root / "missing"), str(root))
    with pytest.raises(PathError, match="outside"):
        check_no_reparse_points(str(outside), str(root))
