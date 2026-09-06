"""Who is asking — decided by the credential, never by the request body.

The quota is worth exactly what `owner` is worth, and `owner` used to be a
string a caller put in the body of an unauthenticated POST. These pin the two
properties that make it worth something: a fixed principal cannot rename itself,
and a delegating principal cannot step outside the prefix it was granted.
"""
import pytest

from livestack_node.fleet_auth import (
    AuthError, Principal, authenticate, bearer_token, fingerprint,
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


def test_resolve_owner_is_pure_and_needs_no_framework():
    fixed = Principal(name="a", owner="a")
    assert resolve_owner(fixed, None) == "a"
    hub = Principal(name="hub", delegate_prefix="acct_")
    assert resolve_owner(hub, "acct_1") == "acct_1"
    assert principal_for(PRINCIPALS, TOK_HUB).name == "hub"
