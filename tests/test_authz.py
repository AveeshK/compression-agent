import pytest

from compression_agent.authz import AuthorizationError, Authorizer, StaticGroupResolver
from compression_agent.config import ConfigError, RootRule, parse_settings

ROOTS = (
    RootRule(r"\\srv\share", frozenset({r"corp\broad"})),
    RootRule(r"\\srv\share\restricted", frozenset({r"corp\narrow"})),
)
RESOLVER = StaticGroupResolver(
    {
        "alice@corp.com": frozenset({r"corp\broad"}),
        "bob@corp.com": frozenset({r"corp\narrow"}),
        "eve@corp.com": frozenset({r"corp\other"}),
    }
)
AUTH = Authorizer(ROOTS, RESOLVER)


def test_member_allowed():
    assert AUTH.authorize("Alice@Corp.com", r"\\srv\share\a").path == r"\\srv\share"


def test_non_member_denied():
    with pytest.raises(AuthorizationError, match="not in a group"):
        AUTH.authorize("eve@corp.com", r"\\srv\share\a")


def test_outside_all_roots_denied():
    with pytest.raises(AuthorizationError, match="not under any allowed root"):
        AUTH.authorize("alice@corp.com", r"\\other\share")


def test_most_specific_root_wins_but_broader_membership_still_counts():
    assert AUTH.authorize("bob@corp.com", r"\\srv\share\restricted\x").path.endswith("restricted")
    # alice is in the broad group, whose root also covers the restricted subtree
    assert AUTH.authorize("alice@corp.com", r"\\srv\share\restricted\x").path == r"\\srv\share"
    with pytest.raises(AuthorizationError):
        AUTH.authorize("bob@corp.com", r"\\srv\share\other")


def test_allowed_roots():
    assert [r.path for r in AUTH.allowed_roots("bob@corp.com")] == [r"\\srv\share\restricted"]
    assert AUTH.allowed_roots("nobody@corp.com") == []


def test_parse_settings():
    s = parse_settings(
        {
            "roots": [{"path": "\\\\srv\\share\\", "groups": ["CORP\\G"]}],
            "zip": {"on_exists": "fail", "compression_level": 3},
            "auth": {"resolver": "static", "static": {"A@x.com": ["CORP\\G"]}},
        }
    )
    assert s.roots[0] == RootRule(r"\\srv\share", frozenset({r"corp\g"}))
    assert s.on_exists == "fail" and s.compression_level == 3
    assert s.static_groups == {"a@x.com": frozenset({r"corp\g"})}


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"roots": [{"path": "C:\\local", "groups": ["g"]}]},
        {"roots": [{"path": "\\\\s\\x", "groups": []}]},
        {"roots": [{"path": "\\\\s\\x", "groups": ["g"]}], "zip": {"on_exists": "nope"}},
    ],
)
def test_parse_settings_rejects(raw):
    with pytest.raises(ConfigError):
        parse_settings(raw)
