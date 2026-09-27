"""Settings loaded from a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from compression_agent.paths import normalize_unc

ON_EXISTS_POLICIES = ("fail", "overwrite", "timestamp")
GIB = 1024**3


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RootRule:
    """An allowed UNC root and the AD groups permitted to zip beneath it."""

    path: str
    groups: frozenset[str]  # casefolded DOMAIN\Name or SID strings


@dataclass(frozen=True)
class Settings:
    seven_zip: Path
    roots: tuple[RootRule, ...]
    db_path: Path
    log_path: Path | None = None
    max_concurrent_jobs: int = 2
    max_queued_per_user: int = 5
    job_timeout_s: int = 180 * 60
    min_free_bytes: int = 10 * GIB
    compression_level: int = 1
    on_exists: str = "timestamp"
    auth_resolver: str = "windows"
    auth_cache_s: int = 600
    static_groups: dict[str, frozenset[str]] = field(default_factory=dict)


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None
    return parse_settings(raw, base_dir=path.parent)


def parse_settings(raw: dict, base_dir: Path = Path(".")) -> Settings:
    limits = raw.get("limits", {})
    zip_cfg = raw.get("zip", {})
    auth = raw.get("auth", {})

    roots = []
    for entry in raw.get("roots", []):
        try:
            root_path = normalize_unc(entry["path"])
        except (KeyError, ValueError) as e:
            raise ConfigError(f"bad [[roots]] entry {entry!r}: {e}") from None
        groups = frozenset(g.casefold() for g in entry.get("groups", []))
        if not groups:
            raise ConfigError(f"root {root_path} has no groups; nobody could use it")
        roots.append(RootRule(root_path, groups))
    if not roots:
        raise ConfigError("at least one [[roots]] entry is required")

    on_exists = zip_cfg.get("on_exists", "timestamp")
    if on_exists not in ON_EXISTS_POLICIES:
        raise ConfigError(f"zip.on_exists must be one of {ON_EXISTS_POLICIES}")

    level = int(zip_cfg.get("compression_level", 1))
    if not 0 <= level <= 9:
        raise ConfigError("zip.compression_level must be 0-9")

    resolver = auth.get("resolver", "windows")
    if resolver not in ("windows", "static"):
        raise ConfigError('auth.resolver must be "windows" or "static"')
    static_groups = {
        user.casefold(): frozenset(g.casefold() for g in groups)
        for user, groups in auth.get("static", {}).items()
    }

    def resolve(p: str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else base_dir / p

    seven_zip = Path(raw.get("seven_zip", r"C:\Program Files\7-Zip\7z.exe"))
    log_path = raw.get("log_path")

    return Settings(
        seven_zip=seven_zip,
        roots=tuple(roots),
        db_path=resolve(raw.get("db_path", "jobs.sqlite3")),
        log_path=resolve(log_path) if log_path else None,
        max_concurrent_jobs=max(1, int(limits.get("max_concurrent_jobs", 2))),
        max_queued_per_user=max(1, int(limits.get("max_queued_per_user", 5))),
        job_timeout_s=int(limits.get("job_timeout_minutes", 180)) * 60,
        min_free_bytes=int(float(limits.get("min_free_gb", 10)) * GIB),
        compression_level=level,
        on_exists=on_exists,
        auth_resolver=resolver,
        auth_cache_s=int(auth.get("cache_minutes", 10)) * 60,
        static_groups=static_groups,
    )
