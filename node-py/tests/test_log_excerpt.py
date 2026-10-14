"""The decisive command.log line of a failed handler (livestack_node/workloads/log_excerpt.py)."""
import subprocess
from pathlib import Path

from livestack_node.workloads import causes
from livestack_node.workloads.log_excerpt import EXCERPT_BYTES, clean, excerpt


def log_of(tmp_path, text):
    path = tmp_path / 'command.log'
    path.write_text(text)
    return path


FIXTURES = Path(__file__).parent / 'fixtures' / 'command_logs'


def test_the_final_failure_block_wins_over_an_early_cascading_error(tmp_path):
    noise = 'cleanup: removing scratch\n' * 5000
    path = log_of(tmp_path, 'starting\nerror[E0432]: unresolved import `x`\n' + noise + 'Error: exit status 1\nstack\n')
    assert excerpt(path, 101) == 'exit 101: Error: exit status 1'


def test_a_block_is_reported_from_its_first_line(tmp_path):
    log = 'FAILED tests/a.py::one - assert 1 == 2\nFAILED tests/a.py::two - KeyError\n=== 2 failed, 5 passed in 0.4s ===\n'
    assert excerpt(log_of(tmp_path, 'collected 7 items\n' + log), 1) == 'exit 1: FAILED tests/a.py::one - assert 1 == 2'


def test_real_flutter_check_failure_not_the_containerd_fsverity_warning():
    # Authority job a12d9930 (benchday.compilation.flutter-check.v1, 2026-10-14): the old picker matched the
    # case-insensitive `FAILED` in containerd's "level=warning ... failed check for fsverity support" on line 1.
    log = FIXTURES / 'flutter_check_a12d9930.txt'
    assert 'fsverity' in log.read_text().splitlines()[8]
    found = excerpt(log, 1)
    assert 'fsverity' not in found
    assert found.startswith('exit 1: command_cause.CommandFailed: flutter test failed with exit 1')
    assert 'Some tests failed' in found


def test_cargo_error_names_the_compile_failure_not_the_warning_before_it(tmp_path):
    log = ('warning: unused variable: `y`\n --> src/lib.rs:3:9\n   Compiling foo v0.1.0\n'
           'error[E0308]: mismatched types\n --> src/lib.rs:7:5\n  |\n7 |     1u8\n  |     ^^^ expected `u32`\n\n'
           'warning: `foo` (lib) generated 1 warning\nerror: could not compile `foo` (lib) due to 1 previous error; 1 warning emitted\n')
    assert excerpt(log_of(tmp_path, log), 101) == 'exit 101: error: could not compile `foo` (lib) due to 1 previous error; 1 warning emitted'


def test_flutter_analyze_reports_the_first_error_of_the_final_block(tmp_path):
    log = ('Analyzing benchday...\n\n  warning \u2022 Unused import \u2022 lib/a.dart:1:8 \u2022 unused_import\n'
           "  error \u2022 Undefined name 'foo' \u2022 lib/b.dart:3:5 \u2022 undefined_identifier\n"
           "  error \u2022 The method 'bar' isn't defined \u2022 lib/c.dart:9:1 \u2022 undefined_method\n\n3 issues found. (ran in 2.1s)\n")
    assert excerpt(log_of(tmp_path, log), 1) == "exit 1: error \u2022 Undefined name 'foo' \u2022 lib/b.dart:3:5 \u2022 undefined_identifier"


def test_test_runner_summary_and_npm_lifecycle(tmp_path):
    log = ('npm WARN deprecated rimraf@3.0.2: no longer supported\n> jest\n  \u25cf adds > works\n\n'
           'Tests:       1 failed, 4 passed, 5 total\nnpm ERR! code ELIFECYCLE\nnpm ERR! errno 1\n')
    assert excerpt(log_of(tmp_path, log), 1) == 'exit 1: Tests: 1 failed, 4 passed, 5 total'


def test_python_traceback_reports_the_exception_not_the_traceback_header(tmp_path):
    log = 'Traceback (most recent call last):\n  File "x.py", line 3, in <module>\n    f()\nValueError: bad input\n'
    assert excerpt(log_of(tmp_path, log), 1) == 'exit 1: ValueError: bad input'


def test_warnings_and_pull_noise_never_win_and_exit_code_lines_beat_the_last_line(tmp_path):
    log = ('Pulling fs layer\n45a1c: Pull complete\nlevel=warning msg="deprecated: failed to FAILED thing"\n'
           'npm WARN FAILED to resolve\nstep 3 returned a non-zero code: exit status 2\n######## 90%\n')
    assert excerpt(log_of(tmp_path, log), 2) == 'exit 2: step 3 returned a non-zero code: exit status 2'
    assert excerpt(log_of(tmp_path, 'building\nwarning: unused thing\n'), 2) == 'exit 2: building'


def test_a_real_node_syntax_error_names_the_file_without_the_worker_prefix(tmp_path):
    script = tmp_path / 'attempt-1' / 'source' / 'scripts' / 'lib' / 'x.mjs'
    script.parent.mkdir(parents=True)
    script.write_text('const a = 1;\nconst a = 2;\n')
    run = subprocess.run(['node', str(script)], capture_output=True, text=True)
    assert run.returncode == 1
    found = excerpt(log_of(tmp_path, run.stderr), run.returncode)
    assert found == "exit 1: SyntaxError: Identifier 'a' has already been declared (at scripts/lib/x.mjs:2)"


def test_secret_lines_are_never_quoted_and_url_credentials_go(tmp_path):
    assert excerpt(log_of(tmp_path, 'Error: bad token abc\n'), 1) == 'exit 1: [line withheld: matches a secret pattern]'
    assert clean('Error: cannot fetch https://user:pw@host/x') == 'Error: cannot fetch https://host/x'
    assert 'AAAA' not in clean('Error: ' + 'AAAA' * 20)


def test_excerpt_is_one_bounded_line(tmp_path):
    found = excerpt(log_of(tmp_path, 'Error: ' + 'é' * 1000 + '\nsecond\n'), 3)
    assert '\n' not in found and len(found.encode()) <= EXCERPT_BYTES


def test_no_decisive_line_falls_back_to_the_last_line_and_empty_log_says_so(tmp_path):
    assert excerpt(log_of(tmp_path, 'a\nlast words\n'), 2) == 'exit 2: last words'
    assert excerpt(log_of(tmp_path, ''), 2) == 'exit 2; command.log had no output'
    assert excerpt(tmp_path / 'missing.log', 2) is None


def test_authority_recleans_what_a_worker_sent_and_derives_the_reason():
    hostile = 'exit 1: Error: ' + 'x y ' * 5000
    cause = causes.derive('product_failure', {'exit_code': 1, 'log_excerpt': hostile})
    assert len(cause['evidence']['excerpt'].encode()) <= EXCERPT_BYTES
    assert causes.reason_of(cause).startswith('handler failed: exit 1: ')
    withheld = causes.derive('product_failure', {'exit_code': 1, 'log_excerpt': 'exit 1: Error: password=hunter2'})
    assert 'hunter2' not in str(withheld)
    bare = causes.derive('product_failure', {'exit_code': 1})
    assert 'excerpt' not in bare['evidence'] and causes.reason_of(bare) is None
