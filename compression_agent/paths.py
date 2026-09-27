"""UNC path normalization and containment checks.

Everything user-supplied goes through normalize_unc() before it is compared
against the allowed roots, and through check_no_reparse_points() before it is
touched, so a request can't escape its root via "..", device paths, or
junctions/symlinks.
"""

from __future__ import annotations

import os
import stat

_INVALID_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}


class PathError(ValueError):
    pass


def _check_component(part: str, raw: str) -> None:
    if part in (".", ".."):
        raise PathError(f"relative components are not allowed: {raw}")
    if bad := _INVALID_CHARS.intersection(part):
        raise PathError(f"invalid character(s) {''.join(sorted(bad))!r} in: {raw}")
    if part != part.rstrip(" ."):
        # Windows silently strips trailing dots/spaces, which would let two
        # different strings name the same folder.
        raise PathError(f"components may not end with a dot or space: {raw}")


def normalize_unc(raw: str) -> str:
    r"""Return a canonical \\server\share[\...] path or raise PathError."""
    s = raw.strip().lstrip("﻿").strip().strip("\"'").replace("/", "\\")
    if s.startswith(("\\\\?\\", "\\\\.\\")):
        raise PathError(f"device paths are not allowed: {raw}")
    if not s.startswith("\\\\"):
        raise PathError(f"not a UNC path (expected \\\\server\\share\\...): {raw}")
    parts = [p for p in s[2:].split("\\") if p]
    if len(parts) < 2:
        raise PathError(f"UNC path must include a server and share: {raw}")
    for part in parts:
        _check_component(part, raw)
    return "\\\\" + "\\".join(parts)


def is_within(path: str, root: str) -> bool:
    """True if normalized `path` is `root` or beneath it (case-insensitive)."""
    # lower() rather than casefold(): Windows doesn't fold e.g. "ß" to "ss".
    p, r = path.lower(), root.lower()
    return p == r or p.startswith(r + "\\")


def join_child(parent: str, name: str) -> str:
    """Join a single folder name onto `parent`, rejecting anything path-like."""
    if not name or "\\" in name or "/" in name:
        raise PathError(f"not a plain folder name: {name!r}")
    _check_component(name, name)
    return parent.rstrip("\\") + "\\" + name


def is_reparse_point(path: str) -> bool:
    attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def check_no_reparse_points(path: str, root: str) -> None:
    """Ensure no component below `root` (down to `path`) is a junction/symlink.

    The root itself may be a DFS link or similar; that is the admin's choice.
    """
    if not is_within(path, root):
        raise PathError(f"{path} is outside {root}")
    root_depth = len(root.split("\\"))
    current = root
    for part in path.split("\\")[root_depth:]:
        current = current + "\\" + part
        try:
            if is_reparse_point(current):
                raise PathError(f"links/junctions are not allowed: {current}")
        except FileNotFoundError:
            raise PathError(f"path does not exist: {current}") from None
