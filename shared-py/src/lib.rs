//! Python binding for the residency planner — a thin pyo3 wrapper over
//! `livestack_shared::residency`. The decision logic lives entirely in the pure
//! `shared` crate; this crate only marshals types across the FFI boundary so
//! `polycore` (Python) can delegate its residency brain to Rust without keeping
//! a parallel implementation. The `shared` crate stays pyo3-free (purity).
//!
//! Built into the `shared_py` extension module via maturin. Plans cross as
//! `(evict, load)` string-list tuples; the host executes the side-effects and
//! reports results back through `commit_*` / `mark_recovered`.

use std::collections::BTreeMap;

use livestack_shared::residency::{Planner as CorePlanner, ResidencyPolicy, UnitMeta};
use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;

/// Residency state machine, callable from Python. Wraps the pure core planner.
#[pyclass]
struct Planner {
    inner: CorePlanner,
}

#[pymethods]
impl Planner {
    /// `units`: list of `(name, footprint, policy_wire, min_resident, has_health_check)`.
    /// `policy_wire` matches the proto ints (0 HARD_PIN / 1 SOFT_PIN / 2 UNPINNED).
    #[new]
    fn new(units: Vec<(String, u64, i64, u32, bool)>) -> Self {
        let mut map = BTreeMap::new();
        for (name, footprint, policy, min_resident, has_health_check) in units {
            map.insert(
                name,
                UnitMeta {
                    footprint,
                    policy: ResidencyPolicy::from_wire(policy),
                    min_resident,
                    has_health_check,
                },
            );
        }
        Planner {
            inner: CorePlanner::new(map),
        }
    }

    fn known(&self, name: &str) -> bool {
        self.inner.known(name)
    }

    fn is_resident(&self, name: &str) -> bool {
        self.inner.is_resident(name)
    }

    fn resident(&self) -> Vec<String> {
        self.inner.resident()
    }

    /// Returns `(evict, load)`. Raises `KeyError` for an unknown unit.
    fn plan_acquire(&self, coload: bool, name: &str) -> PyResult<(Vec<String>, Vec<String>)> {
        self.inner
            .plan_acquire(coload, name)
            .map(|p| (p.evict, p.load))
            .map_err(PyKeyError::new_err)
    }

    fn plan_idle_sweep(&self, idle_seconds: f64, idle_for: f64) -> Vec<String> {
        self.inner.plan_idle_sweep(idle_seconds, idle_for)
    }

    fn probe_candidates(&self) -> Vec<String> {
        self.inner.probe_candidates()
    }

    fn plan_recover(&self, degraded: Vec<String>, now: f64, min_interval: f64) -> Vec<String> {
        self.inner.plan_recover(&degraded, now, min_interval)
    }

    /// Returns `(evict, load)`. Raises `KeyError` for an unknown unit.
    fn plan_recover_one(&self, name: &str) -> PyResult<(Vec<String>, Vec<String>)> {
        self.inner
            .plan_recover_one(name)
            .map(|p| (p.evict, p.load))
            .map_err(PyKeyError::new_err)
    }

    fn commit_loaded(&mut self, name: &str) {
        self.inner.commit_loaded(name);
    }

    fn commit_evicted(&mut self, name: &str) {
        self.inner.commit_evicted(name);
    }

    fn mark_recovered(&mut self, name: &str, now: f64) {
        self.inner.mark_recovered(name, now);
    }

    fn clear_resident(&mut self) -> Vec<String> {
        self.inner.clear_resident()
    }
}

/// The `shared_py` extension module.
#[pymodule]
fn shared_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Planner>()?;
    Ok(())
}
