use std::collections::HashMap;
use livestack_shared::systems::def_graph::{DefGraph, DefGraphNode, DefGraphNodeType};
use livestack_shared::systems::instantiated_graph::InstantiatedGraph;

#[cfg(test)]
mod tests {
    use super::*;

    /// This test corresponds to "should instantiate from a defGraph and create all nodes and edges properly"
    /// in the original TypeScript test. It verifies that nodes and edges are properly created when instantiated.
    #[test]
    fn should_instantiate_from_a_def_graph_and_create_all_nodes_and_edges_properly() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string(), "input2".to_string()];
        let output_tags = vec!["output1".to_string(), "output2".to_string()];
        let mut def_graph = DefGraph::new(root_spec_name.clone(), input_tags, output_tags);

        // Add a spec node to the DefGraph
        let spec_node_data = DefGraphNode {
            node_type: DefGraphNodeType::Spec,
            spec_name: Some("SpecA".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "SpecA".to_string(),
        };
        let _spec_node_id = def_graph.ensure_node("SpecA", spec_node_data);

        // Instantiate an InstantiatedGraph from the DefGraph
        let instantiated_graph = InstantiatedGraph::new(
            "testContext".to_string(),
            "rootJob".to_string(),
            HashMap::new(), // stream_id_overrides
            HashMap::new(), // inlet_has_transform_overrides_by_tag
            HashMap::new(), // stream_source_spec_type_by_stream_id
            &def_graph,
        );

        // Check that a node has job_id "rootJob" (for the RootSpec).
        // Also check that another has job_id "[testContext]SpecA".
        let node_indices = instantiated_graph.node_indices();
        let root_job_found = node_indices.iter().any(|node_id| {
            instantiated_graph.node_weight(*node_id)
                .and_then(|node| node.job_id.as_ref())
                == Some(&"rootJob".to_string())
        });
        let spec_a_found = node_indices.iter().any(|node_id| {
            instantiated_graph.node_weight(*node_id)
                .and_then(|node| node.job_id.as_ref())
                == Some(&"[testContext]SpecA".to_string())
        });
        assert!(root_job_found, "Expected a node with job_id == 'rootJob'.");
        assert!(spec_a_found, "Expected a node with job_id == '[testContext]SpecA'.");

        // Check edge counts match
        let edge_count_in_instantiated_graph = instantiated_graph.edge_count();
        let edge_count_in_def_graph = def_graph.edge_count();
        assert_eq!(
            edge_count_in_instantiated_graph, edge_count_in_def_graph,
            "InstantiatedGraph and DefGraph should have the same number of edges"
        );
    }

    /// This test corresponds to "should instantiate with multiple spec nodes and compute correct job ids"
    /// in the original TypeScript test. It checks whether multiple specs get correct job IDs in the instantiated graph.
    #[test]
    fn should_instantiate_with_multiple_spec_nodes_and_compute_correct_job_ids() {
        let mut def_graph = DefGraph::new(
            "RootSpec".to_string(),
            vec![],  // no input tags
            vec![],  // no output tags
        );

        // Add two spec nodes (SpecA, SpecB) with inlets so they appear as specs in def_graph
        def_graph.ensure_inlet_and_stream(
            livestack_shared::systems::def_graph_utils::SpecTagInfo {
                spec_name: "SpecA".to_string(),
                tag: "dummyA".to_string(),
                unique_spec_label: None,
            },
            false,
        );
        def_graph.ensure_inlet_and_stream(
            livestack_shared::systems::def_graph_utils::SpecTagInfo {
                spec_name: "SpecB".to_string(),
                tag: "dummyB".to_string(),
                unique_spec_label: None,
            },
            false,
        );

        // Instantiate
        let instantiated_graph = InstantiatedGraph::new(
            "multiTest".to_string(),
            "rootJob".to_string(),
            HashMap::new(), // stream_id_overrides
            HashMap::new(), // inlet_has_transform_overrides_by_tag
            HashMap::new(), // stream_source_spec_type_by_stream_id
            &def_graph,
        );

        let node_indices = instantiated_graph.node_indices();
        let found_spec_a = node_indices.iter().any(|id| {
            let node = instantiated_graph.node_weight(*id).unwrap();
            node.job_id.as_deref() == Some("[multiTest]SpecA")
        });
        let found_spec_b = node_indices.iter().any(|id| {
            let node = instantiated_graph.node_weight(*id).unwrap();
            node.job_id.as_deref() == Some("[multiTest]SpecB")
        });

        assert!(found_spec_a, "Expected a node with job_id == '[multiTest]SpecA'.");
        assert!(found_spec_b, "Expected a node with job_id == '[multiTest]SpecB'.");
    }

    /// This test corresponds to "should have the same node count as the underlying defGraph"
    /// in the original TypeScript test. It verifies that InstantiatedGraph has the same number of nodes as DefGraph.
    #[test]
    fn should_have_the_same_node_count_as_the_underlying_def_graph() {
        let mut def_graph = DefGraph::new(
            "RootSpec".to_string(),
            vec!["in1".to_string()],
            vec!["out1".to_string()],
        );

        def_graph.ensure_inlet_and_stream(
            livestack_shared::systems::def_graph_utils::SpecTagInfo {
                spec_name: "SpecA".to_string(),
                tag: "dummy".to_string(),
                unique_spec_label: None,
            },
            false,
        );

        let instantiated_graph = InstantiatedGraph::new(
            "nodeCountTest".to_string(),
            "rootJob".to_string(),
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
            &def_graph,
        );

        let instantiated_node_count = instantiated_graph.node_indices().len();
        let def_graph_node_count = def_graph.node_count();

        assert_eq!(
            instantiated_node_count, def_graph_node_count,
            "InstantiatedGraph should have the same node count as the underlying DefGraph"
        );
    }

    /// This test corresponds to "should retrieve correct node attributes for all nodes"
    /// in the original TypeScript test. Here, we check that every node has a label, and that
    /// job/root-job nodes also have a jobId.
    #[test]
    fn should_retrieve_correct_node_attributes_for_all_nodes() {
        let mut def_graph = DefGraph::new(
            "RootSpec".to_string(),
            vec!["in1".to_string()],
            vec!["out1".to_string()],
        );
        def_graph.ensure_inlet_and_stream(
            livestack_shared::systems::def_graph_utils::SpecTagInfo {
                spec_name: "SpecA".to_string(),
                tag: "dummy".to_string(),
                unique_spec_label: None,
            },
            false,
        );

        let instantiated_graph = InstantiatedGraph::new(
            "attrTest".to_string(),
            "rootJob".to_string(),
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
            &def_graph,
        );

        for node_id in instantiated_graph.node_indices() {
            let node = instantiated_graph.node_weight(node_id).unwrap();
            // The node must have some label
            assert!(
                !node.label.is_empty(),
                "Expected the node to have a non-empty label"
            );

            // If it's a job or root-job type, then job_id should exist
            match node.node_type {
                livestack_shared::systems::instantiated_graph::InstantiatedNodeType::RootJob
                | livestack_shared::systems::instantiated_graph::InstantiatedNodeType::Job => {
                    assert!(
                        node.job_id.is_some(),
                        "Expected job_id to be present for a job or root-job node"
                    );
                }
                _ => {
                    // Other node types won't have a job_id, so we don't check them here
                }
            }
        }
    }
} 