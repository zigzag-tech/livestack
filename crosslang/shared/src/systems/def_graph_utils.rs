use std::collections::HashMap;
use petgraph::graph::NodeIndex;

/// Generates a unique identifier for a stream with optional unique labels for source and destination.
pub fn unique_stream_identifier(
    from_spec_name: Option<&str>,
    from_tag: Option<&str>,
    from_unique_spec_label: Option<&str>,
    to_spec_name: Option<&str>,
    to_tag: Option<&str>,
    to_unique_spec_label: Option<&str>,
) -> String {
    let from_str = match (from_spec_name, from_tag, from_unique_spec_label) {
        (Some(spec_name), Some(tag), Some(label)) => format!("{}[{}]/{}", spec_name, label, tag),
        (Some(spec_name), Some(tag), None) => format!("{}/{}", spec_name, tag),
        _ => "(*)".to_string(),
    };
    let to_str = match (to_spec_name, to_tag, to_unique_spec_label) {
        (Some(spec_name), Some(tag), Some(label)) => format!("{}[{}]/{}", spec_name, label, tag),
        (Some(spec_name), Some(tag), None) => format!("{}/{}", spec_name, tag),
        _ => "(*)".to_string(),
    };
    format!("{}>>{}", from_str, to_str)
}

/// Generates a unique identifier for a spec with an optional unique label.
pub fn unique_spec_identifier(spec_name: &str, unique_spec_label: Option<&str>) -> String {
    match unique_spec_label {
        Some(label) => format!("{}[{}]", spec_name, label),
        None => spec_name.to_string(),
    }
}
