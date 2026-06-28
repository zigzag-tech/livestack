//! Residency decision core — the pure planner behind polycore's `ModelManager`.
//!
//! This is the **functional core**: state in, plan out. It owns the resident-set
//! state machine, idle math, victim selection, and the functional-health recover
//! decision + per-unit rate-limit. It performs **no I/O** — no model load/unload,
//! no GPU calls, no health-probe inference, no clock, no network. The host (the
//! Python polyasr/polytts servers, later TS services) executes the returned
//! [`Plan`] side-effects and reports results back via the `commit_*` / `mark_*`
//! methods. Time is supplied by the caller (`now`, `idle_for`) so the core stays
//! pure and deterministically testable.
//!
//! Mirrors `polycore.ModelManager` + `LocalCoordinator`; the Python package is a
//! thin shim over this. Exposed to Python via the `shared-py` pyo3 crate and
//! (later) to TS via `shared-wasm`, so one brain serves every language.

use std::collections::{BTreeMap, BTreeSet};

/// Per-unit static residency class. Wire ints match the broker proto
/// (`0 HARD_PIN, 1 SOFT_PIN, 2 UNPINNED`) — the single source of truth the TS
/// broker + proto should derive from rather than hand-mirror.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResidencyPolicy {
    HardPin = 0,
    SoftPin = 1,
    Unpinned = 2,
}

impl ResidencyPolicy {
    pub fn from_wire(i: i64) -> ResidencyPolicy {
        match i {
            0 => ResidencyPolicy::HardPin,
            1 => ResidencyPolicy::SoftPin,
            _ => ResidencyPolicy::Unpinned,
        }
    }
    pub fn to_wire(self) -> i64 {
        self as i64
    }
}

/// Declarative metadata a unit reports to the planner/broker. The loader/freer
/// and the health-probe itself live in the host language, never here.
#[derive(Clone, Debug)]
pub struct UnitMeta {
    pub footprint: u64,
    pub policy: ResidencyPolicy,
    pub min_resident: u32,
    pub has_health_check: bool,
}

impl Default for UnitMeta {
    fn default() -> Self {
        UnitMeta {
            footprint: 0,
            policy: ResidencyPolicy::Unpinned,
            min_resident: 0,
            has_health_check: false,
        }
    }
}

/// A set of side-effects for the host to execute, in order: evict then load.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Plan {
    pub evict: Vec<String>,
    pub load: Vec<String>,
}

/// The residency state machine. Holds *which units exist*, *which are resident*,
/// and *when each was last recovered* (rate-limit state). It never holds models,
/// timers, or device handles — those are the host's.
pub struct Planner {
    units: BTreeMap<String, UnitMeta>,
    resident: BTreeSet<String>,
    last_recover: BTreeMap<String, f64>,
}

impl Planner {
    pub fn new(units: BTreeMap<String, UnitMeta>) -> Self {
        Planner {
            units,
            resident: BTreeSet::new(),
            last_recover: BTreeMap::new(),
        }
    }

    pub fn known(&self, name: &str) -> bool {
        self.units.contains_key(name)
    }

    pub fn is_resident(&self, name: &str) -> bool {
        self.resident.contains(name)
    }

    /// Resident units, sorted (parity with `sorted(m.resident)`).
    pub fn resident(&self) -> Vec<String> {
        self.resident.iter().cloned().collect()
    }

    // --- decisions (pure; no mutation) ---------------------------------------

    /// Plan making `name` resident. With `coload=false`, acquiring one unit
    /// evicts every other resident unit (the standalone one-in-VRAM discipline);
    /// with `coload=true`, siblings stay. A resident unit is a no-op. Returns
    /// `Err` if `name` is unknown.
    pub fn plan_acquire(&self, coload: bool, name: &str) -> Result<Plan, String> {
        if !self.units.contains_key(name) {
            return Err(format!("unknown unit: {name}"));
        }
        if self.resident.contains(name) {
            return Ok(Plan::default());
        }
        let evict = if coload {
            Vec::new()
        } else {
            self.resident.iter().filter(|n| *n != name).cloned().collect()
        };
        Ok(Plan {
            evict,
            load: vec![name.to_string()],
        })
    }

    /// Idle sweep victims: ALL resident units when `idle_seconds > 0` and the
    /// session has been idle longer than that. `idle_for` is supplied by the
    /// host (monotonic seconds since last use) to keep this pure.
    pub fn plan_idle_sweep(&self, idle_seconds: f64, idle_for: f64) -> Vec<String> {
        if idle_seconds <= 0.0 || self.resident.is_empty() || idle_for <= idle_seconds {
            return Vec::new();
        }
        self.resident()
    }

    /// Resident units that carry a functional health-probe — the host should run
    /// each probe and feed the unhealthy ones to [`plan_recover`].
    pub fn probe_candidates(&self) -> Vec<String> {
        self.resident
            .iter()
            .filter(|n| self.units.get(*n).map(|u| u.has_health_check).unwrap_or(false))
            .cloned()
            .collect()
    }

    /// Of the host-reported `degraded` units, which to evict+reload now — those
    /// past the per-unit `min_interval` since their last recover (or always when
    /// `min_interval <= 0`). Order follows `degraded`, deduped, resident-only.
    pub fn plan_recover(&self, degraded: &[String], now: f64, min_interval: f64) -> Vec<String> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        for name in degraded {
            if !self.resident.contains(name) || !seen.insert(name.clone()) {
                continue;
            }
            if min_interval > 0.0 {
                let last = self.last_recover.get(name).copied().unwrap_or(f64::NEG_INFINITY);
                if now - last < min_interval {
                    continue; // rate-limited — a still-broken reload must not hot-loop
                }
            }
            out.push(name.clone());
        }
        out
    }

    /// Plan an explicit single-unit recover: evict (if resident) then load.
    pub fn plan_recover_one(&self, name: &str) -> Result<Plan, String> {
        if !self.units.contains_key(name) {
            return Err(format!("unknown unit: {name}"));
        }
        let evict = if self.resident.contains(name) {
            vec![name.to_string()]
        } else {
            Vec::new()
        };
        Ok(Plan {
            evict,
            load: vec![name.to_string()],
        })
    }

    // --- commits (host calls after executing side-effects) -------------------

    pub fn commit_loaded(&mut self, name: &str) {
        if self.units.contains_key(name) {
            self.resident.insert(name.to_string());
        }
    }

    pub fn commit_evicted(&mut self, name: &str) {
        self.resident.remove(name);
    }

    /// Record that `name` was just reloaded for functional degradation, arming
    /// the rate-limit. The reload's load/evict are committed via the usual
    /// `commit_*`; this only stamps the recover time.
    pub fn mark_recovered(&mut self, name: &str, now: f64) {
        self.last_recover.insert(name.to_string(), now);
    }

    /// Force-evict everything (for `unload_now`). Returns the sorted names that
    /// were resident so the host can free them.
    pub fn clear_resident(&mut self) -> Vec<String> {
        let evicted = self.resident();
        self.resident.clear();
        evicted
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn units() -> BTreeMap<String, UnitMeta> {
        let mut m = BTreeMap::new();
        m.insert(
            "asr".to_string(),
            UnitMeta {
                footprint: 4_000_000_000,
                policy: ResidencyPolicy::HardPin,
                min_resident: 1,
                has_health_check: true,
            },
        );
        m.insert(
            "tts".to_string(),
            UnitMeta {
                policy: ResidencyPolicy::SoftPin,
                ..Default::default()
            },
        );
        m.insert("align".to_string(), UnitMeta::default());
        m
    }

    #[test]
    fn wire_ints_match_proto() {
        assert_eq!(ResidencyPolicy::HardPin.to_wire(), 0);
        assert_eq!(ResidencyPolicy::SoftPin.to_wire(), 1);
        assert_eq!(ResidencyPolicy::Unpinned.to_wire(), 2);
        assert_eq!(ResidencyPolicy::from_wire(0), ResidencyPolicy::HardPin);
        assert_eq!(ResidencyPolicy::from_wire(2), ResidencyPolicy::Unpinned);
    }

    #[test]
    fn acquire_loads_once_then_shares() {
        let mut p = Planner::new(units());
        let plan = p.plan_acquire(true, "asr").unwrap();
        assert_eq!(plan.load, vec!["asr"]);
        assert!(plan.evict.is_empty());
        p.commit_loaded("asr");
        // second acquire is a no-op (already resident)
        assert_eq!(p.plan_acquire(true, "asr").unwrap(), Plan::default());
        assert_eq!(p.resident(), vec!["asr"]);
    }

    #[test]
    fn coload_keeps_both() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        let plan = p.plan_acquire(true, "tts").unwrap();
        assert!(plan.evict.is_empty());
        assert_eq!(plan.load, vec!["tts"]);
    }

    #[test]
    fn no_coload_evicts_others() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        let plan = p.plan_acquire(false, "tts").unwrap();
        assert_eq!(plan.evict, vec!["asr"]);
        assert_eq!(plan.load, vec!["tts"]);
    }

    #[test]
    fn acquire_unknown_errs() {
        let p = Planner::new(units());
        assert!(p.plan_acquire(true, "ghost").is_err());
    }

    #[test]
    fn idle_sweep_evicts_all_when_idle() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        assert!(p.plan_idle_sweep(1.0, 0.5).is_empty()); // not yet idle
        assert_eq!(p.plan_idle_sweep(1.0, 5.0), vec!["asr"]); // idle past timeout
        assert!(p.plan_idle_sweep(0.0, 9999.0).is_empty()); // disabled (idle<=0)
    }

    #[test]
    fn clear_resident_returns_sorted() {
        let mut p = Planner::new(units());
        p.commit_loaded("tts");
        p.commit_loaded("asr");
        assert_eq!(p.clear_resident(), vec!["asr", "tts"]);
        assert!(p.resident().is_empty());
        assert!(p.clear_resident().is_empty()); // idempotent
    }

    #[test]
    fn probe_candidates_are_resident_probed_units() {
        let mut p = Planner::new(units());
        assert!(p.probe_candidates().is_empty()); // none resident
        p.commit_loaded("asr"); // has_health_check
        p.commit_loaded("tts"); // no probe
        assert_eq!(p.probe_candidates(), vec!["asr"]);
    }

    #[test]
    fn recover_returns_degraded_resident() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        assert_eq!(
            p.plan_recover(&["asr".to_string()], 100.0, 0.0),
            vec!["asr"]
        );
        // not resident → skipped
        assert!(p.plan_recover(&["align".to_string()], 100.0, 0.0).is_empty());
    }

    #[test]
    fn recover_is_rate_limited() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        let first = p.plan_recover(&["asr".to_string()], 100.0, 600.0);
        assert_eq!(first, vec!["asr"]);
        p.mark_recovered("asr", 100.0);
        // within interval → suppressed
        assert!(p.plan_recover(&["asr".to_string()], 200.0, 600.0).is_empty());
        // past interval → allowed again
        assert_eq!(
            p.plan_recover(&["asr".to_string()], 800.0, 600.0),
            vec!["asr"]
        );
    }

    #[test]
    fn recover_one_evicts_if_resident() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        let plan = p.plan_recover_one("asr").unwrap();
        assert_eq!(plan.evict, vec!["asr"]);
        assert_eq!(plan.load, vec!["asr"]);
        // not resident → load only
        let plan2 = p.plan_recover_one("align").unwrap();
        assert!(plan2.evict.is_empty());
        assert_eq!(plan2.load, vec!["align"]);
    }

    #[test]
    fn recover_dedupes_and_keeps_order() {
        let mut p = Planner::new(units());
        p.commit_loaded("asr");
        p.commit_loaded("tts");
        let got = p.plan_recover(
            &["tts".to_string(), "asr".to_string(), "tts".to_string()],
            1.0,
            0.0,
        );
        assert_eq!(got, vec!["tts", "asr"]);
    }
}
