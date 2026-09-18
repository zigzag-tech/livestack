import pytest

from livestack_node.policy_lab.workflow import WorkflowError, WorkflowGraph, WorkflowNode


def test_s19_earlier_simulated_predecessor_advances_child():
    graph = WorkflowGraph(
        [
            WorkflowNode("parent", (), (), ("artifact",), 0),
            WorkflowNode("child", ("parent",), ("artifact",), (), 25),
        ]
    )
    graph.complete("parent", at_us=100, artifacts={"artifact": ("region-a", 110)})
    assert graph.runnable_at("child") == 135

    later = WorkflowGraph(
        [
            WorkflowNode("parent", (), (), ("artifact",), 0),
            WorkflowNode("child", ("parent",), ("artifact",), (), 25),
        ]
    )
    later.complete("parent", at_us=200, artifacts={"artifact": ("region-a", 210)})
    assert later.runnable_at("child") == 235


def test_artifact_must_be_available_not_merely_named():
    graph = WorkflowGraph(
        [
            WorkflowNode("parent", (), (), ("digest",), 0),
            WorkflowNode("child", ("parent",), ("digest",), (), 0),
        ]
    )
    graph.complete("parent", at_us=10, artifacts={})
    assert graph.runnable_at("child") is None


def test_cycles_and_missing_ancestors_are_rejected():
    with pytest.raises(WorkflowError, match="missing dependency"):
        WorkflowGraph([WorkflowNode("child", ("missing",), (), (), 0)])
    with pytest.raises(WorkflowError, match="cycle"):
        WorkflowGraph(
            [
                WorkflowNode("a", ("b",), (), (), 0),
                WorkflowNode("b", ("a",), (), (), 0),
            ]
        )
