"""What a node announces: a mesh identity is additive to its http one.

meshlink-rollout.md promises "HTTP peers are never deprecated". Announcing only
the mesh name broke that on 2026-09-27: brokers older than MeshPeer could not
dial `mesh://`, and `tower-llm` sat MIA for 17 h. These pin the promise.
"""
from livestack_node.serve import announce_targets


class _Mesh:
    def advertised_url_for(self, prefix):
        return f"mesh://livestack/tower-llm{prefix}"


def test_mesh_node_with_a_reachable_host_announces_both_identities():
    assert announce_targets(_Mesh(), "/livestack", 8188, "100.64.0.18") == [
        ("mesh://livestack/tower-llm/livestack", "http://127.0.0.1:8188/livestack"),
        ("http://100.64.0.18:8188/livestack", None),
    ]


def test_mesh_node_with_unset_host_keeps_the_loopback_http_identity():
    # Unset HOST is what a non-mesh node announces today; turning mesh on must
    # not take that away from the same-machine host broker.
    assert announce_targets(_Mesh(), "/livestack", 8188, None)[1] == (
        "http://127.0.0.1:8188/livestack", None)


def test_host_that_is_the_daemon_id_announces_mesh_only():
    # The operator's explicit statement that this node has no reachable address.
    assert announce_targets(_Mesh(), "/livestack", 8188, "tower-llm") == [
        ("mesh://livestack/tower-llm/livestack", "http://127.0.0.1:8188/livestack"),
    ]


def test_non_mesh_node_is_unchanged():
    assert announce_targets(None, "/livestack", 8188, "100.64.0.18") == [
        ("http://100.64.0.18:8188/livestack", None),
    ]
