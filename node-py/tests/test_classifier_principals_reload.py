"""SIGHUP reloads the classifier principal table without a restart, and a bad
file never replaces a good table.

server.py boots vLLM machinery at import, so the function is lifted out of the
source and run against the real `fleet_auth` -- the part that matters (file
mode, JSON, rejected entries) is the real code, not a fake.
"""
import ast
import json
import os
import pathlib

import pytest

from livestack_node.fleet_auth import load_principals

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "examples" / "harmony-llm" / "server.py").read_text()
TOK_A, TOK_B = "a" * 40, "b" * 40


def _ns(table):
    fn = next(n for n in ast.parse(SRC).body
              if isinstance(n, ast.FunctionDef) and n.name == "_reload_classifier_principals")
    ns = {"_CLASSIFIER_PRINCIPALS": table}
    exec(compile(ast.Module([fn], []), "server.py", "exec"), ns)
    return ns


def _write(path, obj, mode=0o600):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    os.chmod(path, mode)


def _entry(name, prefix):
    return {"name": name, "delegate_prefix": prefix}


@pytest.fixture
def src(tmp_path, monkeypatch):
    f = tmp_path / "principals.json"
    monkeypatch.setenv("LIVESTACK_FLEET_TOKENS_FILE", str(f))
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS", raising=False)
    return f


def test_a_valid_file_is_swapped_in_and_logged_without_tokens(src, capsys):
    old = load_principals(json.dumps({TOK_A: _entry("hub", "benchday:")}))
    _write(src, {TOK_A: _entry("hub", "benchday:"), TOK_B: _entry("media-corpus", "media-corpus")})
    ns = _ns(old)
    msg = ns["_reload_classifier_principals"]()
    assert msg.startswith("swapped") and "added: media-corpus" in msg
    assert {p.name for p in ns["_CLASSIFIER_PRINCIPALS"].values()} == {"hub", "media-corpus"}
    out = capsys.readouterr().out
    assert "principals reload: swapped" in out
    assert TOK_A not in out and TOK_B not in out


@pytest.mark.parametrize("make", [
    lambda f: f.write_text("{not json"),                                  # malformed
    lambda f: (_write(f, {}), None)[1],                                   # empty
    lambda f: _write(f, {TOK_B: _entry("x", "x:")}, mode=0o644),          # world-readable
    lambda f: _write(f, {TOK_B: _entry("x", "x:"), "short": _entry("y", "y:")}),  # partial
    lambda f: None,                                                       # missing file
])
def test_a_bad_file_keeps_the_old_table(src, capsys, make):
    old = load_principals(json.dumps({TOK_A: _entry("hub", "benchday:")}))
    make(src)
    ns = _ns(old)
    msg = ns["_reload_classifier_principals"]()
    assert msg.startswith("KEPT old table"), msg
    assert ns["_CLASSIFIER_PRINCIPALS"] is old
    assert "principals reload: KEPT old table" in capsys.readouterr().out


def test_no_configured_source_keeps_the_old_table(monkeypatch):
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS_FILE", raising=False)
    monkeypatch.delenv("LIVESTACK_FLEET_TOKENS", raising=False)
    old = load_principals(json.dumps({TOK_A: _entry("hub", "benchday:")}))
    ns = _ns(old)
    assert ns["_reload_classifier_principals"]().startswith("KEPT old table")
    assert ns["_CLASSIFIER_PRINCIPALS"] is old


def test_sighup_is_handled_so_it_cannot_kill_the_service():
    assert "signal.signal(signal.SIGHUP" in SRC
