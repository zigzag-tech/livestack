use serde::{Serialize};
#[derive(Serialize, Debug, PartialEq, Clone)]
pub struct SpecTagInfo {
    pub spec_name: String,
    pub tag: String,
    pub unique_spec_label: Option<String>,
}

#[derive(Serialize, Debug, PartialEq, Clone)]
pub struct FromSpecAndTag {
    pub spec_name: String,
    pub output: String,
    pub unique_spec_label: Option<String>,
}

#[derive(Serialize, Debug, PartialEq, Clone)]
pub struct ToSpecAndTag {
    pub spec_name: String,
    pub input: String,
    pub has_transform: bool,
    pub unique_spec_label: Option<String>,
}

/*
JS implementation for reference:
export function uniqueStreamIdentifier({
    from,
    to,
  }: {
    from?: {
      specName: string;
      tag: string;
      uniqueSpecLabel?: string;
    };
    to?: {
      specName: string;
      tag: string;
      uniqueSpecLabel?: string;
    };
  }) {
    const fromStr = !!from
      ? `${from.specName}${
          from.uniqueSpecLabel && from.uniqueSpecLabel !== "default_label"
            ? `(${from.uniqueSpecLabel})`
            : ""
        }/${from.tag}`
      : "(*)";
    const toStr = !!to
      ? `${to.specName}${
          to.uniqueSpecLabel && to.uniqueSpecLabel !== "default_label"
            ? `(${to.uniqueSpecLabel})`
            : ""
        }/${to.tag}`
      : "(*)";
    return `${fromStr}>>${toStr}`;
  }
   */

/// Generates a unique identifier for a stream with optional unique labels for source and destination.
// #[napi]
pub fn unique_stream_identifier(from: Option<SpecTagInfo>, to: Option<SpecTagInfo>) -> String {
    let from_str = match from {
        Some(from) => {
            format!(
                "{}{}{}",
                from.spec_name,
                match from.unique_spec_label {
                    Some(label) => {
                        if label != "default_label" {
                            format!("({})", label)
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
                "{}{}{}",
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


/// Generates a unique identifier for a spec with an optional unique label.
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


/*
JS implementation for reference:
export function uniqueSpecIdentifier({
  specName,
  spec,
  uniqueSpecLabel,
}: {
  specName?: string;
  spec?: { name: string };
  uniqueSpecLabel?: string;
}) {
  specName = specName ?? spec?.name;
  if (!specName) {
    throw new Error("specName or spec must be provided");
  }
  return `${specName}${uniqueSpecLabel ? `[${uniqueSpecLabel}]` : ""}`;
}

*/