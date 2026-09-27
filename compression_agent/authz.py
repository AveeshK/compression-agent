"""Requester authorization: AD group membership checked against allowed roots."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Protocol

from compression_agent.config import RootRule, Settings
from compression_agent.paths import is_within


class AuthorizationError(PermissionError):
    pass


class GroupResolver(Protocol):
    def groups_for(self, user: str) -> frozenset[str]:
        """Casefolded group names (DOMAIN\\Name) and/or SIDs for `user`."""
        ...


class StaticGroupResolver:
    """Fixed user -> groups mapping; for tests and non-domain machines."""

    def __init__(self, mapping: dict[str, frozenset[str]]):
        self._mapping = {u.casefold(): g for u, g in mapping.items()}

    def groups_for(self, user: str) -> frozenset[str]:
        return self._mapping.get(user.casefold(), frozenset())


# WindowsIdentity(upn) performs an S4U logon, which returns the user's full
# transitive group list without needing their password or any special
# privilege. Requires this machine to be joined to (or trust) the user's domain.
# The UPN is passed via an environment variable so it is never parsed as code.
_S4U_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$id = [System.Security.Principal.WindowsIdentity]::new($env:ZIPAGENT_UPN)
$out = foreach ($g in $id.Groups) {
    $g.Value
    try { $g.Translate([System.Security.Principal.NTAccount]).Value } catch {}
}
ConvertTo-Json -Compress -InputObject @($out)
"""


class WindowsGroupResolver:
    """Resolves transitive AD groups for a UPN via S4U, with a TTL cache."""

    def __init__(self, cache_seconds: int = 600, timeout_s: int = 30):
        self._cache: dict[str, tuple[float, frozenset[str]]] = {}
        self._lock = threading.Lock()
        self._ttl = cache_seconds
        self._timeout = timeout_s

    def groups_for(self, user: str) -> frozenset[str]:
        key = user.casefold()
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
        groups = self._lookup(user)
        with self._lock:
            self._cache[key] = (time.monotonic(), groups)
        return groups

    def _lookup(self, user: str) -> frozenset[str]:
        if "@" not in user:
            raise AuthorizationError(f"expected a UPN (user@domain), got {user!r}")
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _S4U_SCRIPT],
            env={**os.environ, "ZIPAGENT_UPN": user},
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if proc.returncode != 0:
            raise AuthorizationError(
                f"could not resolve AD groups for {user}: {proc.stderr.strip()[:300]}"
            )
        return frozenset(g.casefold() for g in json.loads(proc.stdout or "[]"))


def make_resolver(settings: Settings) -> GroupResolver:
    if settings.auth_resolver == "static":
        return StaticGroupResolver(settings.static_groups)
    return WindowsGroupResolver(cache_seconds=settings.auth_cache_s)


class Authorizer:
    def __init__(self, roots: tuple[RootRule, ...], resolver: GroupResolver):
        self._roots = roots
        self._resolver = resolver

    def allowed_roots(self, user: str) -> list[RootRule]:
        groups = self._resolver.groups_for(user)
        return [r for r in self._roots if r.groups & groups]

    def authorize(self, user: str, path: str) -> RootRule:
        """Return the most specific root covering `path` that `user` may use.

        `path` must already be normalized.
        """
        covering = sorted(
            (r for r in self._roots if is_within(path, r.path)),
            key=lambda r: len(r.path),
            reverse=True,
        )
        if not covering:
            raise AuthorizationError(f"{path} is not under any allowed root")
        groups = self._resolver.groups_for(user)
        for rule in covering:
            if rule.groups & groups:
                return rule
        raise AuthorizationError(f"{user} is not in a group permitted for {path}")
