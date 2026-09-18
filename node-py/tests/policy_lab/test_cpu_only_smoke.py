import builtins
import json
import subprocess
import sys


def test_import_and_replay_do_not_import_gpu_or_runtime_modules(monkeypatch):
    forbidden = ("torch", "cuda", "livestack_node._native", "requests", "httpx")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in forbidden or name.startswith(tuple(f"{item}." for item in forbidden)):
            raise AssertionError(f"offline smoke imported forbidden module: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    from livestack_node.policy_lab import replay_smoke

    manifest = {
        "schema_version": 1,
        "events": [
            {"event_id": "later", "virtual_time_us": 4, "event_phase": 0},
            {"event_id": "arrival", "virtual_time_us": 1, "event_phase": 2},
            {"event_id": "finish", "virtual_time_us": 1, "event_phase": 1},
        ],
    }
    first = replay_smoke(manifest)
    second = replay_smoke(manifest)
    assert first == second
    assert first["event_ids"] == ["finish", "arrival", "later"]


def test_replay_smoke_cli_works_without_network_or_weights(tmp_path):
    manifest = tmp_path / "smoke.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {"event_id": "one", "virtual_time_us": 0, "event_phase": 2}
                ],
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "livestack_node.policy_lab", "replay-smoke", str(manifest)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["event_ids"] == ["one"]
