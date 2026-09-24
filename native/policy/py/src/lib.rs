//! Python module `livestack_policy`. Its `policy` submodule is the Jingway glue
//! (`jingway_policy_py`) bound to [`LivestackFamilies`]: `load_artifact`,
//! `decide(artifact, family_id, ctx: dict, candidates: list[dict], decision_id)`,
//! `Recorder`, `families`. The decision dict carries the family's evaluation
//! unchanged in `rows` (eligible / score / explorable / reason per candidate).

use livestack_policy_family::LivestackFamilies;
use pyo3::prelude::*;

#[pymodule]
fn livestack_policy(m: &Bound<'_, PyModule>) -> PyResult<()> {
    jingway_policy_py::add_policy_module(m.py(), m, &LivestackFamilies)
}
