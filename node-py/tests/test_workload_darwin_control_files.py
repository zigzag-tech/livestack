import json
import plistlib
from pathlib import Path

from livestack_node.workloads import darwin_supervision


def test_hosted_runner_keeps_launchd_control_files_off_the_workspace(monkeypatch, tmp_path):
    workspace = tmp_path/'apfs-workspace'
    output = workspace/'attempt'/'output'
    state = tmp_path/'runner-temp'/'worker-state'/'launchd'
    executor = darwin_supervision.LaunchdExecutor('github-actions-ios', state_dir=state)
    attempt = 'a'*32
    reports = iter((None, {'state':'running', 'pid':1234}))
    monkeypatch.setattr(darwin_supervision, 'launchd_job', lambda *_args, **_kwargs: next(reports))
    commands = []
    monkeypatch.setattr(executor, 'command', lambda *args, **_kwargs: commands.append(args))

    executor.start(attempt, ['/usr/bin/python3', 'handler.py'], workspace/'source', output,
                   env={'PATH':'/usr/bin:/bin'}, cpu=3, memory_bytes=1024**3,
                   max_seconds=60, tasks=32)

    assert len(commands) == 1
    plist_path = Path(commands[0][-1])
    assert plist_path.is_relative_to(state)
    assert not plist_path.is_relative_to(workspace)
    plist = plistlib.loads(plist_path.read_bytes())
    assert 'WorkingDirectory' not in plist
    config_path = Path(plist['ProgramArguments'][2])
    assert config_path.is_relative_to(state)
    config = json.loads(config_path.read_text())
    assert config['cwd'] == str((workspace/'source').resolve())
    assert config['output'] == str(output.resolve())

    monkeypatch.setattr(darwin_supervision, 'launchd_job', lambda *_args, **_kwargs: None)
    executor.stop(attempt)
    assert not plist_path.exists()
    assert not config_path.exists()
