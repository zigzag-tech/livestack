from livestack_node.policy_lab.policy_sandbox import PolicySandbox


def test_valid_policy_runs_with_bounded_json_output():
    sandbox = PolicySandbox(cpu_ms=200, memory_bytes=128 * 1024 * 1024, output_bytes=1024)
    result = sandbox.run(
        "def decide(observation):\n    return {'chosen': observation['candidate']}\n",
        {"candidate": "worker"},
    )
    assert result.status == "ok"
    assert result.output == {"chosen": "worker"}


def test_infinite_loop_hits_runtime_budget_deterministically():
    sandbox = PolicySandbox(cpu_ms=50, memory_bytes=128 * 1024 * 1024, output_bytes=1024)
    result = sandbox.run("def decide(observation):\n    while True:\n        pass\n", {})
    assert result.status == "runtime_budget"


def test_arbitrary_file_and_network_import_access_are_unavailable():
    sandbox = PolicySandbox(cpu_ms=200, memory_bytes=128 * 1024 * 1024, output_bytes=1024)
    file_result = sandbox.run(
        "def decide(observation):\n    return open('/etc/passwd').read()\n", {}
    )
    network_result = sandbox.run(
        "def decide(observation):\n    return __import__('socket').socket()\n", {}
    )
    assert file_result.status == "invalid_policy"
    assert network_result.status == "invalid_policy"


def test_output_budget_is_enforced():
    sandbox = PolicySandbox(cpu_ms=200, memory_bytes=128 * 1024 * 1024, output_bytes=100)
    result = sandbox.run("def decide(observation):\n    return {'x': 'a' * 1000}\n", {})
    assert result.status == "output_budget"
