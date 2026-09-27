"""List the immediate subfolders of a directory.

Deliberately shallow: walking massive trees to compute sizes over SMB is the
slow thing this tool exists to avoid.
"""

import os
import stat
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FolderEntry:
    name: str
    path: str
    modified: datetime


def list_subfolders(path: str) -> list[FolderEntry]:
    """Subfolders of `path`, sorted by name. Junctions/symlinks are skipped."""
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            if not entry.is_dir(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            entries.append(
                FolderEntry(entry.name, entry.path, datetime.fromtimestamp(st.st_mtime))
            )
    entries.sort(key=lambda e: e.name.lower())
    return entries
