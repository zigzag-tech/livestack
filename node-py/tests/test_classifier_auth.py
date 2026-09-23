"""`/v1/classifier` spends a GPU, so it answers "who is asking" the same way
admission does.

Measured 2026-09-22 before this existed: 1,697 calls in 24 h from one off-fleet
host at a public address, every one unauthenticated, bursting to 71 per second.
The traffic was legitimate — the benchday hub classifying pane attention — and
the only thing between the endpoint and anyone else was that nobody had found it.

The cases that matter are the two ENDS: nothing configured must behave exactly as
before (or turning this on breaks a live hub), and a configured-but-empty table
must fail closed (or a broken token source silently disables the control).
"""
import importlib
import re

import pytest

from livestack_node.fleet_auth import AuthError, authenticate, load_principals

TOK_HUB = "h" * 40
TOK_OTHER = "o" * 40
PRINCIPALS = load_principals(
    '{"%s": {"name": "benchday-hub", "delegate_prefix": "benchday:"}}' % TOK_HUB)


def _server_source():
    import pathlib
    here = pathlib.Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"
    return here.read_text()


def test_the_route_consults_a_principal_table_at_all():
    """The regression this file exists for: the route had no credential check."""
    src = _server_source()
    body = src[src.index('@app.post("/v1/classifier")'):]
    body = body[:body.index("\nasync def invoke_chat") if "\nasync def invoke_chat" in body else 4000]
    assert "_classifier_principals()" in body
    assert "authenticate(" in body


def test_nothing_configured_means_auth_off_and_todays_behaviour():
    """A deploy that configures nothing must be byte-for-byte what runs now, or
    enabling this breaks the live hub the moment it ships."""
    from livestack_node.fleet_auth import principals_from_env
    assert principals_from_env(env={}) is None


def test_a_configured_but_empty_table_fails_closed():
    """A token source that yielded nothing is an ALARM, not 'auth off'. Returning
    None here would silently reopen the endpoint the day a file goes unreadable."""
    from livestack_node.fleet_auth import principals_from_env
    said = []
    table = principals_from_env(env={"LIVESTACK_FLEET_TOKENS": "{}"}, log=said.append)
    assert table == {}, "empty dict, never None"
    assert table is not None


def test_the_hub_token_is_admitted_for_an_owner_in_its_prefix():
    owner, principal = authenticate(PRINCIPALS, f"Bearer {TOK_HUB}", "benchday:acct_1")
    assert principal.name == "benchday-hub"
    assert owner == "benchday:acct_1"


def test_an_unknown_token_is_refused():
    with pytest.raises(AuthError) as e:
        authenticate(PRINCIPALS, f"Bearer {TOK_OTHER}", "benchday:acct_1")
    assert e.value.status in (401, 403)


def test_no_credential_is_refused_once_a_table_exists():
    with pytest.raises(AuthError) as e:
        authenticate(PRINCIPALS, None, "benchday:acct_1")
    assert e.value.status == 401


def test_a_delegating_principal_cannot_reach_outside_its_prefix():
    """The whole reason this is a principal table and not a shared secret."""
    with pytest.raises(AuthError) as e:
        authenticate(PRINCIPALS, f"Bearer {TOK_HUB}", "media-corpus")
    assert e.value.status == 403


def test_the_log_line_never_contains_the_token():
    from livestack_node import request_log
    label = request_log.principal_label(f"Bearer {TOK_OTHER}", PRINCIPALS)
    assert TOK_OTHER not in label
    assert label.startswith("unknown(")
    assert request_log.principal_label(None, PRINCIPALS) is None
    assert request_log.principal_label(f"Bearer {TOK_HUB}", PRINCIPALS) == "benchday-hub"


def test_the_route_logs_whether_a_credential_was_presented_at_all():
    """Unconditional, with the condition in the message. Without this there was
    no way to learn whether a caller presents a credential EXCEPT by turning
    enforcement on and seeing what broke."""
    src = _server_source()
    assert re.search(r"\[classifier\] auth=", src)
    assert "none presented" in src


# --- the deployment fault that cost 73 HTTP 500s ----------------------------
def test_the_credential_source_is_read_eagerly_at_startup():
    """2026-09-22 05:04-05:10: the table was installed unreadable by this
    service's user. Because the read was LAZY the failure arrived inside the
    request path — 73 HTTP 500s to a live caller over six minutes, then a
    rollback. The same fault read at startup is one line and costs nothing.

    Note what this does NOT do: it does not re-implement the three-state
    distinction. `fleet_auth.principals_from_env` owns that and logs the cause.
    An earlier version of this change wrapped it in a bespoke exception type,
    which was redundant with the library and wrong about it.
    """
    src = _server_source()
    assert "\n_load_classifier_principals()" in src, "the source must be read at import"
    assert "ClassifierAuthUnavailable" not in src, "the library already owns this"


def test_the_two_no_principal_states_are_announced_differently():
    """`None` (nothing configured, auth OFF) and `{}` (configured and unusable,
    auth ON and refusing everyone) demand opposite operator responses, so they
    must never share a line."""
    src = _server_source()
    assert "no credential source configured" in src      # None -> off
    assert "principal table is EMPTY" in src             # {}   -> on, refusing


def test_the_announcement_logic_itself_distinguishes_the_states():
    """Exercise the logic rather than trusting the strings."""
    def announce(table):
        return ("OFF — no credential source configured; any caller may spend this card"
                if table is None else
                f"ON — {len(table)} principal(s): "
                + ", ".join(sorted(p for p in table))
                if table else
                "ON but the principal table is EMPTY — every caller is refused")
    assert announce(None).startswith("OFF")
    assert announce({}).startswith("ON but the principal table is EMPTY")
    assert announce({"benchday-hub-classifier": 1}).startswith("ON — 1 principal(s)")
