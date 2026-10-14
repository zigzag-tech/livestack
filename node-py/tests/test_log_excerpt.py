"""The decisive command.log line of a failed handler (livestack_node/workloads/log_excerpt.py)."""
import subprocess

from livestack_node.workloads import causes
from livestack_node.workloads.log_excerpt import EXCERPT_BYTES, clean, excerpt


def log_of(tmp_path, text):
    path = tmp_path / 'command.log'
    path.write_text(text)
    return path


def test_first_error_line_wins_over_post_failure_noise(tmp_path):
    noise = 'cleanup: removing scratch\n' * 5000
    path = log_of(tmp_path, 'starting\nerror[E0432]: unresolved import `x`\n' + noise + 'Error: exit status 1\n')
    assert excerpt(path, 101) == 'exit 101: error[E0432]: unresolved import `x`'


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
