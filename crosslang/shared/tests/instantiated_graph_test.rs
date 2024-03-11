use crate::systems::def_graph::{DefGraph, DefGraphNode, NodeType};
use crate::systems::instantiated_graph::{InstantiatedGraph, InstantiatedGraphNode, InstantiatedNodeType};
use petgraph::graph::NodeIndex;
use std::collections::HashMap;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn should_instantiate_from_a_def_graph_and_create_all_nodes_and_edges_properly() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string(), "input2".to_string()];
        let output_tags = vec!["output1".to_string(), "output2".to_string()];
        let def_graph = DefGraph::new(root_spec_name.clone(), input_tags, output_tags);

        // Add a spec node to the DefGraph
        let spec_node_data = DefGraphNode {
            node_type: NodeType::Spec,
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
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
            def_graph,
        );

        // Check if all nodes from the DefGraph are present in the InstantiatedGraph
        let nodes = instantiated_graph.graph.node_indices().map(|index| {
            instantiated_graph.graph.node_weight(index).unwrap().clone()
        }).collect::<Vec<_>>();
        assert!(nodes.iter().any(|node| node.job_id.as_deref() == Some("rootJob")));
        assert!(nodes.iter().any(|node| node.job_id.as_deref() == Some("[testContext]SpecA")));

        // Check if all edges from the DefGraph are present in the InstantiatedGraph
        let edges = instantiated_graph.graph.raw_edges();
        assert_eq!(edges.len(), def_graph.graph.edge_count());
    }
}
