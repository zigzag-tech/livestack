"""A worker label that comes from a probe of the host, not from a person (probed_labels.py)."""
import pytest

from livestack_node.workloads import probed_labels
from livestack_node.workloads.probed_labels import ProbedLabels, REPROBE_SECONDS


def fake_which(present):
    return lambda tool: f'/usr/bin/{tool}' if tool in present else None


def test_label_true_only_when_every_tool_resolves():
    tools = {'native_performance': ['Xvfb', 'weston', 'xdotool']}
    assert ProbedLabels(tools, which=fake_which({'Xvfb', 'weston', 'xdotool'})).labels() == {'native_performance': 'true'}
    missing = ProbedLabels(tools, which=fake_which({'xdotool'}))
    assert missing.labels() == {'native_performance': 'false'}
    assert missing.missing == {'native_performance': ['Xvfb', 'weston']}


def test_probe_beats_a_static_label_and_keeps_the_others():
    probed = ProbedLabels({'native_performance': ['Xvfb']}, which=fake_which(set())).labels()
    assert probed_labels.merged({'os': 'linux', 'native_performance': 'true'}, probed) == {'os': 'linux', 'native_performance': 'false'}


def test_installing_the_tool_is_seen_after_the_reprobe_interval():
    present, now = set(), [0.0]
    probe = ProbedLabels({'native_performance': ['Xvfb']}, which=lambda t: '/x' if t in present else None, clock=lambda: now[0])
    assert probe.labels() == {'native_performance': 'false'}
    present.add('Xvfb')
    now[0] = REPROBE_SECONDS - 1
    assert probe.labels() == {'native_performance': 'false'}, 'cached inside the interval'
    now[0] = REPROBE_SECONDS
    assert probe.labels() == {'native_performance': 'true'}


def test_no_label_tools_reports_nothing_extra():
    assert ProbedLabels(None).labels() == {}


@pytest.mark.parametrize('bad', [[], {'x': []}, {'x': ['/usr/bin/Xvfb']}, {'': ['Xvfb']}, {'x': 'Xvfb'}])
def test_invalid_label_tools_are_refused(bad):
    with pytest.raises(ValueError):
        ProbedLabels(bad)


def test_selector_matches_the_probed_label_in_placement():
    """Placement compares selector values to the REPORTED labels by equality (placement.py)."""
    report_labels = probed_labels.merged({'os': 'linux'}, ProbedLabels({'native_performance': ['Xvfb']}, which=fake_which(set())).labels())
    selector = {'os': 'linux', 'native_performance': 'true'}
    assert not all(report_labels.get(k) == v for k, v in selector.items())
