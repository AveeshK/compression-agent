import os
import subprocess
from pathlib import Path

import pytest

SEVEN_ZIP = Path(r"C:\Program Files\7-Zip\7z.exe")

requires_7z = pytest.mark.skipif(not SEVEN_ZIP.is_file(), reason="7-Zip not installed")


def to_unc(path: Path) -> str:
    """C:\\foo -> \\\\localhost\\C$\\foo, so tests exercise real UNC paths."""
    drive, rest = os.path.splitdrive(str(path.resolve()))
    return f"\\\\localhost\\{drive[0].upper()}${rest}"


def make_junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True)  # fmt: skip


@pytest.fixture
def unc_tmp(tmp_path: Path) -> tuple[Path, str]:
    unc = to_unc(tmp_path)
    if not os.path.isdir(unc):
        pytest.skip("admin share \\\\localhost\\C$ not reachable")
    return tmp_path, unc
