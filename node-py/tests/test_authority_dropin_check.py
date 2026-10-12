"""tools/check-authority-dropins.sh: a drop-in sorting after the deploy script's own that sets PYTHONPATH is refused."""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'tools' / 'check-authority-dropins.sh'
MINE = 'zzzzzzzzzzzzzzzz-release-current.conf'


def run(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return subprocess.run([str(SCRIPT), str(tmp_path), MINE], capture_output=True, text=True)


def test_clean_when_the_deploy_dropin_is_last(tmp_path):
    r = run(tmp_path, {'zzzz-release-main-9fddd795.conf': '[Service]\nEnvironment=PYTHONPATH=/a\n', MINE: '[Service]\nEnvironment=PYTHONPATH=/b\n'})
    assert r.returncode == 0, r.stdout


def test_later_pythonpath_dropin_is_refused_and_named(tmp_path):
    r = run(tmp_path, {MINE: '[Service]\nEnvironment=PYTHONPATH=/b\n',
                       'zzzzzzzzzzzzzzzzz-release-roster-verdict.conf': '[Service]\nEnvironment=PYTHONPATH=/c\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'})
    assert r.returncode == 1
    assert 'zzzzzzzzzzzzzzzzz-release-roster-verdict.conf' in r.stdout


def test_later_dropin_without_pythonpath_and_superseded_files_are_fine(tmp_path):
    r = run(tmp_path, {MINE: '[Service]\nEnvironment=PYTHONPATH=/b\n', 'zzzzzzzzzzzzzzzzzz-memory.conf': '[Service]\nMemoryHigh=1G\n',
                       'zzzzzzzzzzzzzzzzz-release-roster-verdict.conf.superseded-x': '[Service]\nEnvironment=PYTHONPATH=/c\n'})
    assert r.returncode == 0, r.stdout


def test_quoted_environment_form_is_caught(tmp_path):
    r = run(tmp_path, {MINE: '', 'zzzzzzzzzzzzzzzzz-x.conf': '[Service]\nEnvironment="A=1" "PYTHONPATH=/c"\n'})
    assert r.returncode == 1
