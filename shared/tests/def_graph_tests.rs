use livestack_shared::systems::def_graph::{DefGraph, DefGraphNode, NodeType,load_from_json};

#[cfg(test)]
mod tests {
    use super::*;
    use assert_matches::assert_matches;
    use livestack_shared::systems::def_graph_utils::{FromSpecAndTag, SpecTagInfo, ToSpecAndTag};
    use petgraph::graph::NodeIndex;

    #[test]
    fn test_get_inbound_stream_nodes() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        // ... (rest of the test remains unchanged)
        let spec_name = "TestSpec";
        let tag = "inputTag";
        let has_transform = true;

        // Ensure inlet and stream nodes are created
        let (inlet_node_id, stream_node_id) =
            graph.ensure_inlet_and_stream(
                SpecTagInfo {
                    spec_name: spec_name.to_string(),
                    tag: tag.to_string(),
                    unique_spec_label: None,
                },
                has_transform);

        // Add another inlet node with a different tag
        let other_tag = "otherInputTag";
        let (other_inlet_node_id, other_stream_node_id) =
            graph.ensure_inlet_and_stream(SpecTagInfo {
                spec_name: spec_name.to_string(),
                tag: other_tag.to_string(),
                unique_spec_label: None,
            
            }, has_transform);

        // Retrieve inbound node sets for the spec node
        let spec_node_id = graph
            .find_node(|attrs| {
                attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
            })
            .expect("Spec node should exist");

        let inbound_node_sets = graph.get_inbound_stream_nodes(spec_node_id);

        // Ensure that the correct inlet and stream nodes are included
        assert_eq!(inbound_node_sets.len(), 2);
        assert!(inbound_node_sets.contains(&(inlet_node_id, stream_node_id)));
        assert!(inbound_node_sets.contains(&(other_inlet_node_id, other_stream_node_id)));
    }

    #[test]
    fn test_ensure_inlet_and_stream() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let spec_name = "TestSpec";
        let tag = "inputTag";
        let has_transform = true;

        // Ensure inlet and stream nodes are created
        let (inlet_node_id, stream_node_id) =
            graph.ensure_inlet_and_stream(
                SpecTagInfo {
                    spec_name: spec_name.to_string(),
                    tag: tag.to_string(),
                    unique_spec_label: None,
                }
                , has_transform);
        

        // Check if the spec node is created
        let spec_node_id = graph
            .find_node(|attrs| {
                attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
            })
            .expect("Spec node should exist");

        // Check if the inlet node is created
        assert_matches!(graph.node_weight(inlet_node_id), Some(node) if node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag) && node.has_transform == Some(has_transform));

        // Check if the inlet node is connected to the spec node
        assert!(
            graph.contains_edge(inlet_node_id, spec_node_id),
            "Inlet node should be connected to the spec node"
        );

        // Check if the stream node is created and connected to the inlet node
        assert_matches!(graph.node_weight(stream_node_id), Some(node) if node.node_type == NodeType::StreamDef && node.stream_def_id == Some(format!("(*)>>{}/{}", spec_name, tag)));
        assert!(
            graph.contains_edge(stream_node_id, inlet_node_id),
            "Stream node should be connected to the inlet node"
        );
    }

    #[test]
    fn test_get_spec_node_ids() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);

        // Add a spec node
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
        let spec_node_id = graph.ensure_node("SpecA", spec_node_data);

        // Add a non-spec node
        let non_spec_node_data = DefGraphNode {
            node_type: NodeType::StreamDef,
            spec_name: None,
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: Some("StreamA".to_string()),
            alias: None,
            direction: None,
            label: "StreamA".to_string(),
        };
        let non_spec_node_id = graph.ensure_node("StreamA", non_spec_node_data);

        // Retrieve spec node IDs
        let spec_node_ids = graph.get_spec_node_ids();
        assert!(spec_node_ids.contains(&spec_node_id));
        assert!(!spec_node_ids.contains(&non_spec_node_id));
    }

    #[test]
    fn test_ensure_node() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);

        // Ensure a node is created
        let test_node = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("TestSpec".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "TestNode".to_string(),
        };
        let node_id = graph.ensure_node("TestNode", test_node.clone());
        
        assert_matches!(graph.node_weight(node_id), Some(node) if &node == &test_node);

        // Ensure the same node is retrieved with the same ID
        let same_node_id = graph.ensure_node("TestNode", test_node.clone());
        assert_eq!(node_id, same_node_id);

        // Ensure the node data is not overwritten if the same ID is used
        // assign a different value to the `label` field
        let different_test_node = DefGraphNode {
            label: "DifferentTestNode".to_string(),
            ..test_node.clone()
        };

        let same_node_id_with_different_data =
            graph.ensure_node("TestNode", different_test_node.clone());
        assert_eq!(
            node_id, same_node_id_with_different_data,
            "The node data should not be overwritten if the same ID is used",
        );
        let node_data = graph.node_weight(same_node_id_with_different_data);

        assert_matches!(
            node_data,
            Some(node) if &node == &test_node,
            "The node data should not be overwritten if the same ID is used",
        );
    }

    #[test]
    fn test_ensure_outlet_and_stream() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let spec_name = "TestSpec";
        let tag = "outputTag";

        // Ensure outlet and stream nodes are created
        let (outlet_node_id, stream_node_id) = graph.ensure_outlet_and_stream(
            SpecTagInfo {
                spec_name: spec_name.to_string(),
                tag: tag.to_string(),
                unique_spec_label: None,
            },
        );
        

        // Check if the spec node is created
        let spec_node_id = graph
            .find_node(|attrs| {
                attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
            })
            .expect("Spec node should exist");

        // Check if the outlet node is created
        assert_matches!(graph.node_weight(outlet_node_id), Some(node) if node.node_type == NodeType::Outlet && node.tag.as_deref() == Some(tag));

        // Check if the outlet node is connected to the spec node
        assert!(
            graph.contains_edge(spec_node_id, outlet_node_id),
            "Outlet node should be connected to the spec node"
        );

        // Check if the stream node is created and connected to the outlet node
        assert_matches!(graph.node_weight(stream_node_id), Some(node) if node.node_type == NodeType::StreamDef && node.stream_def_id == Some(format!("{}/{}>>(*)", spec_name, tag)));
        assert!(
            graph.contains_edge(outlet_node_id, stream_node_id),
            "Stream node should be connected to the outlet node"
        );
    }

    #[test]
    fn test_ensure_edge() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let from_node = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("FromSpec".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "FromNode".to_string(),
        };
        let to_node = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("ToSpec".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "ToNode".to_string(),
        };
        let from_index = graph.ensure_node("FromNode", from_node);
        let to_index = graph.ensure_node("ToNode", to_node);

        graph.ensure_edge(from_index, to_index);
        
        assert!(
            graph.contains_edge(from_index, to_index),
            "Edge should exist from FromNode to ToNode"
        );
        // graph.ensure_edge(from_index, to_index);
        // let edges: Vec<_> = graph.edges_connecting(from_index, to_index).collect();
        // assert_eq!(
        //     edges.len(),
        //     1,
        //     "There should only be one edge from FromNode to ToNode"
        // );
    }

    #[test]
    fn test_filter_inbound_neighbors() {
        let root_spec_name = "RootSpec";
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name.to_string(), input_tags, output_tags);
        let spec_name = "TestSpec";

        let tag = "inputTag";
        let has_transform = true;
        // Ensure inlet and stream nodes are created
        let (inlet_node_id, _) =
            graph.ensure_inlet_and_stream(
                SpecTagInfo {
                    spec_name: spec_name.to_string(),
                    tag: tag.to_string(),
                    unique_spec_label: None,
                }
                , has_transform);
            
        // Add another inlet node with a different tag
        let other_tag = "otherInputTag";
        let (_, other_stream_node_id) =
            graph.ensure_inlet_and_stream(SpecTagInfo{
                spec_name: spec_name.to_string(),
                tag: other_tag.to_string(),
                unique_spec_label: None,
            }, has_transform);
        // Filter inbound neighbors for the spec node with a specific tag
        let spec_node_id = graph
            .find_node(|attrs| {
                attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
            })
            .expect("Spec node should exist");
        let filtered_inbound_neighbors = graph.filter_inbound_neighbors(spec_node_id, |node| {
            node.node_type == NodeType::Inlet && node.tag.as_deref() == Some(tag)
        });
        assert_eq!(filtered_inbound_neighbors.len(), 1);
        let inlet_node_id = NodeIndex::new(inlet_node_id as usize);
        assert_eq!(filtered_inbound_neighbors[0], inlet_node_id);
        // Ensure that the other inlet node is not included
        let other_stream_node_id = NodeIndex::new(other_stream_node_id as usize);
        assert!(!filtered_inbound_neighbors.contains(&other_stream_node_id));
    }
    #[test]
    fn test_get_outbound_stream_nodes() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let spec_name = "TestSpec";
        let tag = "outputTag";

        // Ensure outlet and stream nodes are created
        let (outlet_node_id, stream_node_id) = graph.ensure_outlet_and_stream(
            SpecTagInfo {
                spec_name: spec_name.to_string(),
                tag: tag.to_string(),
                unique_spec_label: None,
            },
        );

        // Retrieve outbound node sets for the spec node
        let spec_node_id = graph
            .find_node(|attrs| {
                attrs.node_type == NodeType::Spec && attrs.spec_name.as_deref() == Some(spec_name)
            })
            .expect("Spec node should exist");

        let outbound_node_sets = graph.get_outbound_stream_nodes(spec_node_id);

        // Ensure that the correct outlet and stream nodes are included
        assert_eq!(outbound_node_sets.len(), 1);

        assert!(outbound_node_sets.contains(&(outlet_node_id, stream_node_id)));
    }

    #[test]
    fn should_assign_an_alias_and_be_able_to_look_it_up() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string()];
        let output_tags = vec!["output1".to_string()];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let spec_name = "SpecA";
        let alias = "AliasA";
        let tag = "input1";
        let type_ = "in";

        // Ensure inlet and stream nodes are created for SpecA
        graph.ensure_inlet_and_stream(
            SpecTagInfo {
                spec_name: spec_name.to_string(),
                tag: tag.to_string(),
                unique_spec_label: None,
            }
            , false);

        // Assign an alias to the inlet node
        graph.assign_alias(alias, spec_name, "RootSpec", None, type_, tag);

        // Look up the alias
        let found_alias = graph.lookup_root_spec_alias(
            spec_name.to_string(),
            None,
             tag.to_string(), 
             type_.to_string()
        );

        assert_eq!(found_alias, Some(alias.to_string()));
    }
    #[test]
    fn should_add_connected_dual_specs_and_ensure_correct_connections() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec![];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        // let from = ("SpecA", "outputA", Some("uniqueA"));
        // let to = ("SpecB", "inputB", true, Some("uniqueB"));
        let from = FromSpecAndTag {
            spec_name: "SpecA".to_string(),
            output: "outputA".to_string(),
            unique_spec_label: Some("uniqueA".to_string()),
        };
        let to = ToSpecAndTag {
            spec_name: "SpecB".to_string(),
            input: "inputB".to_string(),
            has_transform: true,
            unique_spec_label: Some("uniqueB".to_string()),
        };

        // Add connected dual specs
        let (
            from_spec_node_id,
            to_spec_node_id,
            stream_node_id,
            from_outlet_node_id,
            to_inlet_node_id,
        ) = graph.add_connected_dual_specs(&from, &to);

        // Verify the nodes and connections
        
        assert_matches!(graph.node_weight(from_spec_node_id), Some(node) if node.node_type == NodeType::Spec && node.spec_name == Some(from.spec_name) && node.unique_spec_label == from.unique_spec_label);
        assert_matches!(graph.node_weight(to_spec_node_id), Some(node) if node.node_type == NodeType::Spec && node.spec_name == Some(to.spec_name) && node.unique_spec_label == to.unique_spec_label);
        assert_matches!(graph.node_weight(stream_node_id), Some(node) if node.node_type == NodeType::StreamDef);
        assert_matches!(graph.node_weight(from_outlet_node_id), Some(node) if node.node_type == NodeType::Outlet && node.tag == Some(from.output));
        assert_matches!(graph.node_weight(to_inlet_node_id), Some(node) if node.node_type == NodeType::Inlet && node.tag == Some(to.input) && node.has_transform == Some(to.has_transform));

        // Verify the edges
        assert!(
            graph
                .contains_edge(from_spec_node_id, from_outlet_node_id),
            "Edge should exist from from_spec_node_id to from_outlet_node_id"
        );
        assert!(
            graph
                .contains_edge(from_outlet_node_id, stream_node_id),
            "Edge should exist from from_outlet_node_id to stream_node_id"
        );
        assert!(
            graph.contains_edge(stream_node_id, to_inlet_node_id),
            "Edge should exist from stream_node_id to to_inlet_node_id"
        );
        assert!(
            graph.contains_edge(to_inlet_node_id, to_spec_node_id),
            "Edge should exist from to_inlet_node_id to to_spec_node_id"
        );
    }

    #[test]
    fn should_lookup_spec_and_tag_by_alias() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string()];
        let output_tags = vec!["output1".to_string()];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let spec_name = "SpecA";
        let alias = "AliasA";
        let tag = "input1";
        let direction = "in";

        // Ensure inlet and stream nodes are created for SpecA
        graph.ensure_inlet_and_stream(SpecTagInfo{
            spec_name: spec_name.to_string(),
            tag: tag.to_string(),
            unique_spec_label: None,
        
        }, false);

        // Assign an alias to the inlet node
        graph.assign_alias(alias, spec_name, "RootSpec", None, direction, tag);

        // Look up the spec and tag by alias
        let result = graph.lookup_spec_and_tag_by_alias(alias.to_string(), direction);

        assert_eq!(
            result,
            Some(SpecTagInfo {
                spec_name: spec_name.to_string(),
                tag: tag.to_string(),
                unique_spec_label: None
            })
        );
    }
    #[test]
    fn should_serialize_and_deserialize_a_def_graph_resulting_in_an_identical_graph() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string(), "input2".to_string()];
        let output_tags = vec!["output1".to_string(), "output2".to_string()];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);

        // Add some nodes and edges to the graph
        let spec_node_a = DefGraphNode {
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
        let spec_node_a_id = graph.ensure_node("SpecA", spec_node_a);

        let spec_node_b = DefGraphNode {
            node_type: NodeType::Spec,
            spec_name: Some("SpecB".to_string()),
            unique_spec_label: None,
            tag: None,
            has_transform: None,
            stream_def_id: None,
            alias: None,
            direction: None,
            label: "SpecB".to_string(),
        };
        let spec_node_b_id = graph.ensure_node("SpecB", spec_node_b);

        graph.ensure_edge(spec_node_a_id, spec_node_b_id);

        // Serialize the graph to JSON
        let json = graph.to_json().unwrap();

        // Deserialize the JSON back into a graph
        let new_graph = load_from_json(json);

        // Compare the original graph with the deserialized graph
        assert_eq!(new_graph.node_count(), graph.node_count());
        assert_eq!(new_graph.edge_count(), graph.edge_count());

        // Ensure that all nodes and edges are equal
        for index in graph.get_all_node_indices() {
            let index = index.index() as u32;
            let node_weight = graph.node_weight(index).unwrap();
            let new_node_weight = new_graph.node_weight(index).unwrap();
            assert_eq!(node_weight, new_node_weight);
        }

        let new_graph_edges: Vec<_> = new_graph.raw_edges();

        for edge in graph.raw_edges() {
            let new_edge = *(new_graph_edges
                .iter()
                .find(|e| e.0 == edge.0 && e.1 == edge.1)
                .unwrap());
            assert_eq!(edge, new_edge);
        }
    }

    #[test]
    fn should_reuse_existing_stream_def_node_if_outlet_already_connected() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec![];
        let output_tags = vec!["outputA".to_string()];
        let mut graph = DefGraph::new(root_spec_name, input_tags, output_tags);
        let from = FromSpecAndTag {
            spec_name: "SpecA".to_string(),
            output: "outputA".to_string(),
            unique_spec_label: None,
        };
        let to_first = ToSpecAndTag {
            spec_name: "SpecB".to_string(),
            input: "inputB".to_string(),
            has_transform: false,
            unique_spec_label: None,
        };
        let to_second = ToSpecAndTag {
            spec_name: "SpecC".to_string(),
            input: "inputC".to_string(),
            has_transform: false,
            unique_spec_label: None,
        };

        // Connect SpecA to SpecB
        let (_, _, stream_node_id_first, from_outlet_node_id, _) =
            graph.add_connected_dual_specs(&from, &to_first);

        // Connect SpecA to SpecC, which should reuse the existing stream def node
        let (_, _, stream_node_id_second, _, _) =
            graph.add_connected_dual_specs(&from, &to_second);

        // The stream node IDs should be the same, indicating reuse of the stream def node
        assert_eq!(stream_node_id_first, stream_node_id_second);
        // Verify that the from_outlet_node_id is connected to the reused stream_node_id
        assert!(graph.contains_edge(from_outlet_node_id, stream_node_id_first));
    }

    #[test]
    fn should_get_root_spec_node_id_after_initialization() {
        let root_spec_name = "RootSpec".to_string();
        let input_tags = vec!["input1".to_string()];
        let output_tags = vec!["output1".to_string()];
        let graph = DefGraph::new(root_spec_name, input_tags, output_tags);

        let root_spec_node_id = graph.get_root_spec_node_id();


        assert!(root_spec_node_id.is_some());
    }
}