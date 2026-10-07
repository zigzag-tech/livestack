"""Runtime discovery probes the real interpreters on this host (real subprocess, no fakes)."""
import sys
import shutil

from livestack_node.workloads import runtime_discovery as rd
from livestack_node.workloads.handler_installer import HandlerPackageStore


def test_python3_is_discovered_with_its_real_version():
    outcome = rd.probe('python3')
    assert outcome['path'] and outcome['version'].startswith('Python 3.')


def test_a_missing_interpreter_is_a_named_reason_not_silence():
    outcome = rd.probe('node22', which=lambda name: None)
    assert outcome == {'reason': 'node not found on PATH'}


def test_a_too_old_interpreter_is_refused_with_its_version(tmp_path):
    old = tmp_path / 'node'
    old.write_text('#!/bin/sh\necho v20.1.0\n')
    old.chmod(0o755)
    outcome = rd.probe('node22', which=lambda name: str(old))
    assert 'v20.1' in outcome['reason'] and '>= 22' in outcome['reason']


def test_a_configured_entry_wins_and_discovery_only_adds_the_rest(tmp_path):
    runtimes, outcomes = rd.discover({'python3': '/opt/pinned/python3'})
    assert runtimes['python3'] == '/opt/pinned/python3' and outcomes['python3']['source'] == 'config'
    assert outcomes['node22']['source'] == 'discovered'


def test_the_package_store_advertises_discovered_python3_without_config(tmp_path):
    store = HandlerPackageStore(tmp_path / 'releases', {}, {})
    assert 'python3' in store.runtimes and store.runtime_discovery['python3']['source'] == 'discovered'
    assert store.runtimes['python3'] == str(__import__('pathlib').Path(shutil.which('python3')).resolve())
