use serde::Serialize;
#[derive(Serialize, Debug, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SpecTagInfo {
    pub spec_name: String,
    pub tag: String,
    pub unique_spec_label: Option<String>,
}

#[derive(Serialize, Debug, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
pub struct FromSpecAndTag {
    pub spec_name: String,
    pub output: String,
    pub unique_spec_label: Option<String>,
}

#[derive(Serialize, Debug, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
pub struct ToSpecAndTag {
    pub spec_name: String,
    pub input: String,
    pub has_transform: bool,
    pub unique_spec_label: Option<String>,
}

/// Generates a unique identifier for a stream with optional unique labels for source and destination.
// #[napi]
pub fn unique_stream_identifier(from: Option<SpecTagInfo>, to: Option<SpecTagInfo>) -> String {
    let from_str = match from {
        Some(from) => {
            format!(
                "{}{}/{}",
                from.spec_name,
                match from.unique_spec_label {
                    Some(label) => {
                        if label != "default_label" {
                            format!("[{})", label)
                        } else {
                            "".to_string()
                        }
                    }
                    None => "".to_string(),
                },
                from.tag
            )
        }
        None => "(*)".to_string(),
    };
    let to_str = match to {
        Some(to) => {
            format!(
                "{}{}/{}",
                to.spec_name,
                match to.unique_spec_label {
                    Some(label) => {
                        if label != "default_label" {
                            format!("({})", label)
                        } else {
                            "".to_string()
                        }
                    }
                    None => "".to_string(),
                },
                to.tag
            )
        }
        None => "(*)".to_string(),
    };
    format!("{}>>{}", from_str, to_str)
}


/// Generateszz a unique identifier for a spec with an optional unique label.
pub fn unique_spec_identifier(spec_name: String, unique_spec_label: Option<String>) -> String {
    format!(
        "{}{}",
        spec_name,
        match unique_spec_label {
            Some(label) => format!("[{}]", label),
            None => "".to_string(),
        }
    )
}

