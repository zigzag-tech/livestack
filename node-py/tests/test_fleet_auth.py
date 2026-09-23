"""Who is asking — decided by the credential, never by the request body.

The quota is worth exactly what `owner` is worth, and `owner` used to be a
string a caller put in the body of an unauthenticated POST. These pin the two
properties that make it worth something: a fixed principal cannot rename itself,
and a delegating principal cannot step outside the prefix it was granted.
"""
import os

import pytest

from livestack_node.fleet_auth import (
    RELAY_ANY, AuthError, Principal, authenticate, bearer_token, fingerprint,
    load_principals, principal_for, resolve_owner,
)

TOK_FIXED = "t" * 40
TOK_HUB = "h" * 40

CONFIG = (
    '{"%s": {"name": "media-corpus", "owner": "media-corpus"},'
    ' "%s": {"name": "hub", "delegate_prefix": "acct_"}}' % (TOK_FIXED, TOK_HUB)
)
PRINCIPALS = load_principals(CONFIG)


def _auth(token, owner=None):
    return authenticate(PRINCIPALS, f"Bearer {token}" if token else None, owner)


# -- the property the quota rests on -----------------------------------------

def test_a_fixed_principal_is_charged_its_own_owner_whatever_the_body_says():
    owner, who = _auth(TOK_FIXED)
    assert owner == "media-corpus"
    assert who.name == "media-corpus"


def test_a_fixed_principal_naming_another_owner_is_REFUSED_not_overridden():
    """Silent substitution is how a caller ends up debugging the wrong account's
    quota: it believes it is spending X and the broker charges Y, and nobody
    finds out until someone reads the ledger months later."""
    with pytest.raises(AuthError) as e:
        _auth(TOK_FIXED, "someone-else")
    assert e.value.status == 403
    assert "cannot name 'someone-else'" in e.value.detail


def test_naming_its_own_owner_is_fine():
    # Harmless and common — a caller that fills the field consistently.
    assert _auth(TOK_FIXED, "media-corpus")[0] == "media-corpus"


def test_no_token_is_401_and_an_unknown_token_is_401():
    for header in (None, "", "Bearer nope", "Basic abc", "Bearer"):
        with pytest.raises(AuthError) as e:
            authenticate(PRINCIPALS, header, None)
        assert e.value.status == 401, header


# -- delegation, and the prefix that bounds it -------------------------------

def test_a_delegating_principal_may_name_an_owner_inside_its_prefix():
    owner, who = _auth(TOK_HUB, "acct_c082baa1")
    assert owner == "acct_c082baa1"
    assert who.name == "hub"
    assert who.delegates


def test_a_delegating_principal_cannot_step_outside_its_prefix():
    """The prefix is what stops one compromised delegating caller from being
    every account — without it, a hub token spends media-corpus's quota by
    asking for it."""
    with pytest.raises(AuthError) as e:
        _auth(TOK_HUB, "media-corpus")
    assert e.value.status == 403
    assert "may only act for owners starting with 'acct_'" in e.value.detail


def test_a_delegating_principal_must_name_someone():
    """It has no identity of its own to charge, so an unnamed request would have
    to be charged to the hub itself — which is one account holding every
    tenant's usage, i.e. no quota at all."""
    with pytest.raises(AuthError) as e:
        _auth(TOK_HUB)
    assert e.value.status == 400
    assert "must name an `owner`" in e.value.detail


# -- the credential itself ---------------------------------------------------

def test_a_token_is_compared_in_constant_time():
    """A dict lookup answers faster for a wrong token than a right one, and that
    difference leaks the token a character at a time."""
    import inspect
    from livestack_node import fleet_auth
    src = inspect.getsource(fleet_auth.principal_for)
    assert "compare_digest" in src
    assert "principals[" not in src, "a direct lookup reintroduces the side channel"


def test_a_fingerprint_identifies_a_token_without_disclosing_it():
    fp = fingerprint(TOK_FIXED)
    assert len(fp) == 8
    assert TOK_FIXED[:8] not in fp
    assert fingerprint(TOK_FIXED) == fp, "stable, so it is usable in a log"
    assert fingerprint(TOK_HUB) != fp


def test_bearer_parsing_is_strict_about_the_scheme():
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    assert bearer_token("Bearer  abc  ") == "abc"
    assert bearer_token("Basic abc") is None
    assert bearer_token("abc") is None
    assert bearer_token(None) is None


# -- config, which is the thing that will be mistyped ------------------------

def test_a_malformed_entry_is_skipped_without_taking_the_others_with_it():
    lines = []
    p = load_principals(
        '{"%s": {"owner": "ok"}, "short": {"owner": "x"},'
        ' "%s": "not-an-object"}' % ("a" * 40, "b" * 40), log=lines.append)
    assert len(p) == 1
    assert p["a" * 40].owner == "ok"
    assert any("shorter than 16" in l for l in lines)
    assert any("not an object" in l for l in lines)
    # A credential must never appear in a log — the one place people paste.
    assert not any("short" in l and "aaaa" in l for l in lines)


def test_a_principal_that_is_both_fixed_and_delegating_is_refused():
    """Guessing which one was meant would be guessing at a security boundary."""
    lines = []
    p = load_principals(
        '{"%s": {"owner": "a", "delegate_prefix": "acct_"}}' % ("c" * 40),
        log=lines.append)
    assert p == {}
    assert any("BOTH owner and delegate_prefix" in l for l in lines)


def test_a_principal_that_names_neither_is_refused():
    lines = []
    p = load_principals('{"%s": {"name": "x"}}' % ("d" * 40), log=lines.append)
    assert p == {}
    assert any("could act as anyone" in l for l in lines)


def test_unparseable_config_loads_nothing_and_says_what_systemd_does():
    lines = []
    assert load_principals("{not json}", log=lines.append) == {}
    assert any("systemd strips bare double quotes" in l for l in lines)
    # Nothing loaded means every caller gets 401 — a closed door, not an open one.
    with pytest.raises(AuthError) as e:
        authenticate({}, f"Bearer {TOK_FIXED}", None)
    assert e.value.status == 401


def test_an_empty_config_is_empty_not_an_error():
    assert load_principals("") == {}
    assert load_principals("   ") == {}


# -- where the config comes from ----------------------------------------------
#
# The file wins over the inline env because the one secret in the system should
# not be the one setting that is inline JSON — systemd strips bare double
# quotes, and `systemctl show` exposes inline values to every local process.

from livestack_node.fleet_auth import principals_from_env


def test_tokens_file_wins_and_refuses_mode(tmp_path, monkeypatch):
    f = tmp_path / "tokens.json"
    f.write_text(CONFIG)
    os.chmod(f, 0o600)
    monkeypatch.setenv("LIVESTACK_FLEET_TOKENS_FILE", str(f))
    # Inline env set to something DIFFERENT: it must lose to the file.
    monkeypatch.setenv(
        "LIVESTACK_FLEET_TOKENS",
        '{"%s": {"name": "inline-only", "owner": "inline-only"}}' % ("i" * 40))

    got = principals_from_env()
    assert got == PRINCIPALS, "the file wins over the inline env"
    assert "inline-only" not in {p.name for p in got.values()}

    # World-readable (any group/other bit) → refused, loudly, and failing
    # CLOSED: no principals at all, so every caller 401s until the operator
    # fixes the permissions. A disclosed credential must not keep authorizing.
    os.chmod(f, 0o644)
    lines = []
    assert principals_from_env(log=lines.append) == {}
    assert any("chmod 0600" in l for l in lines)
    assert any("Refusing" in l for l in lines)
    # The file path is fine to log; the TOKENS inside it never are.
    assert not any(TOK_FIXED in l for l in lines)

    # Group-readable-only is refused too — the check is "any group/other bit".
    os.chmod(f, 0o640)
    assert principals_from_env(log=lines.append) == {}

    # No file variable at all → the inline env is used (today's behaviour).
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS_FILE")
    inline = principals_from_env()
    assert inline["i" * 40].name == "inline-only"

    # A set-but-missing file fails closed rather than silently falling back to
    # the inline env — a typo in the path must not weaken authentication.
    monkeypatch.setenv("LIVESTACK_FLEET_TOKENS_FILE", str(tmp_path / "gone.json"))
    lines = []
    assert principals_from_env(log=lines.append) == {}
    assert any("cannot be read" in l for l in lines)

    # Nothing configured at all is a THIRD state: None, meaning auth is OFF.
    # It must not be confused with the empty table, which fails closed.
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS_FILE")
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS")
    assert principals_from_env() is None


def test_resolve_owner_is_pure_and_needs_no_framework():
    fixed = Principal(name="a", owner="a")
    assert resolve_owner(fixed, None) == "a"
    hub = Principal(name="hub", delegate_prefix="acct_")
    assert resolve_owner(hub, "acct_1") == "acct_1"
    assert principal_for(PRINCIPALS, TOK_HUB).name == "hub"


# -- the engine principal: relays any owner, and says so ----------------------
#
# harmony-llm, polytts and polyasr admit under the owner their CALLER asserted
# (`X-Harmony-Owner`), and their callers are every application on the fleet. No
# prefix bounds that set. The plan's principal table therefore writes the
# engines as delegating with an empty prefix — and an empty prefix is exactly
# what a typo looks like, so the loader refuses it and the wildcard `*` is the
# one spelling that works. These pin both halves.

TOK_ENGINE = "e" * 40


def test_an_empty_delegate_prefix_is_refused_and_names_the_wildcard():
    """The six engine tokens minted for R.2 carried `"delegate_prefix": ""` and
    every one was dropped at load, silently turning six callers into 401s. The
    refusal stays — but it now tells the operator what to write instead."""
    lines = []
    got = load_principals(
        '{"%s": {"name": "harmony-llm@host", "delegate_prefix": ""}}' % TOK_ENGINE,
        log=lines.append)
    assert got == {}
    assert any("could act as anyone" in l and '"*"' in l for l in lines)


def test_the_wildcard_prefix_loads_relays_any_owner_and_is_loud():
    lines = []
    table = load_principals(
        '{"%s": {"name": "harmony-llm@host", "delegate_prefix": "*"}}' % TOK_ENGINE,
        log=lines.append)
    who = table[TOK_ENGINE]
    assert who.delegates and who.delegate_prefix == RELAY_ANY
    assert any("RELAYS ANY OWNER" in l for l in lines), \
        "a principal as strong as every app token must be visible in the journal"

    # Any owner passes, from any application namespace...
    for owner in ("attune:acct_7", "benchday:acct_1", "media-corpus", "sorbonne"):
        assert authenticate(table, f"Bearer {TOK_ENGINE}", owner) == (owner, who)

    # ...but it still has no identity of its own to charge: an engine that
    # forgot to relay an owner is a bug, not an anonymous grant.
    with pytest.raises(AuthError) as e:
        authenticate(table, f"Bearer {TOK_ENGINE}", None)
    assert e.value.status == 400


def test_the_wildcard_is_not_a_prefix_match_on_a_star():
    """`*` is a sentinel, not a character an owner may start with: a bounded
    principal granted the literal prefix `*` would be a different (and absurd)
    thing, and the sentinel must not leak into ordinary prefix matching."""
    bounded = Principal(name="hub", delegate_prefix="attune:")
    with pytest.raises(AuthError) as e:
        resolve_owner(bounded, "*anything")
    assert e.value.status == 403


def test_a_file_that_stats_but_cannot_be_opened_fails_closed_rather_than_raising(tmp_path):
    """`os.stat` succeeds on a file this process may not READ — a 0600 file owned
    by another user in a traversable directory is exactly that shape, and it is
    what a credential table looks like when it is installed as root for a service
    running as somebody else.

    The docstring promises `{}` and fail-closed for every unusable source. An
    uncaught PermissionError broke that promise in the worst place: measured
    2026-09-22, it surfaced as 73 HTTP 500s from a live endpoint over six
    minutes instead of 401s and one startup line.
    """
    import os
    from livestack_node.fleet_auth import principals_from_env

    path = tmp_path / "principals.json"
    path.write_text('{"%s": {"name": "x", "owner": "x"}}' % ("t" * 40))
    os.chmod(path, 0o600)

    real_open = open

    def refuse(file, *a, **kw):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(file, *a, **kw)

    said = []
    import builtins
    builtins.open = refuse
    try:
        table = principals_from_env(
            env={"LIVESTACK_FLEET_TOKENS_FILE": str(path)}, log=said.append)
    finally:
        builtins.open = real_open

    assert table == {}, "an unreadable source must fail closed, not raise"
    assert table is not None, "{} and None mean opposite things"
    joined = " ".join(said)
    assert "cannot be opened" in joined
    assert "readable by the user this service runs as" in joined
