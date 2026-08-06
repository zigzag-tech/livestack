#![allow(non_snake_case)]

//! WASM/TS bindings for the route-selection core ([`livestack_shared::route`]).
//!
//! The algorithm lives in `livestack-shared` and is bound — never reimplemented —
//! per language: this crate for TS/Node, `shared-py` for Python, FFI for Dart.
//! That is the whole point: three clients previously forked "pick the fastest
//! reachable endpoint" three ways in one language; re-forking it across three
//! languages would be strictly worse.
//!
//! The wire shapes below are compile-checked mirrors of the core types — the
//! `From` impls stop compiling if a core field changes, so the TS surface cannot
//! silently drift from the engine. Field names stay `snake_case` to match the
//! route manifest itself (`target_id`, `health_url`, `regional_relay`).

use livestack_shared::route::{
    candidates_from_entries as candidates_from_entries_impl, classify_direct_host,
    host_of as host_of_impl, is_lan_host as is_lan_host_impl, is_mesh_host as is_mesh_host_impl,
    is_private_host as is_private_host_impl, parse_direct_endpoint as parse_direct_endpoint_impl,
    parse_route_manifest as parse_route_manifest_impl, AddressClass as AddressClassImpl,
    EndpointPolicy as EndpointPolicyImpl, Picker as PickerImpl, PickerConfig as PickerConfigImpl,
    RouteCandidate as RouteCandidateImpl, RouteEntry as RouteEntryImpl, RouteKind as RouteKindImpl,
    RouteObservation as RouteObservationImpl, RouteSnapshot as RouteSnapshotImpl,
    RouteState as RouteStateImpl,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use tsify::Tsify;
use wasm_bindgen::prelude::*;

// ---------------------------------------------------------------------------
// Wire types
// ---------------------------------------------------------------------------

#[derive(Tsify, Serialize, Deserialize, Clone, Copy)]
#[tsify(into_wasm_abi, from_wasm_abi)]
#[serde(rename_all = "snake_case")]
pub enum RouteKind {
    Direct,
    RegionalRelay,
    HubProxy,
    WebRtcP2p,
}

impl From<RouteKindImpl> for RouteKind {
    fn from(k: RouteKindImpl) -> Self {
        match k {
            RouteKindImpl::Direct => RouteKind::Direct,
            RouteKindImpl::RegionalRelay => RouteKind::RegionalRelay,
            RouteKindImpl::HubProxy => RouteKind::HubProxy,
            RouteKindImpl::WebRtcP2p => RouteKind::WebRtcP2p,
        }
    }
}

impl From<RouteKind> for RouteKindImpl {
    fn from(k: RouteKind) -> Self {
        match k {
            RouteKind::Direct => RouteKindImpl::Direct,
            RouteKind::RegionalRelay => RouteKindImpl::RegionalRelay,
            RouteKind::HubProxy => RouteKindImpl::HubProxy,
            RouteKind::WebRtcP2p => RouteKindImpl::WebRtcP2p,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone, Copy)]
#[tsify(into_wasm_abi, from_wasm_abi)]
#[serde(rename_all = "snake_case")]
pub enum RouteState {
    Eligible,
    Cooling,
    Quarantined,
}

impl From<RouteStateImpl> for RouteState {
    fn from(s: RouteStateImpl) -> Self {
        match s {
            RouteStateImpl::Eligible => RouteState::Eligible,
            RouteStateImpl::Cooling => RouteState::Cooling,
            RouteStateImpl::Quarantined => RouteState::Quarantined,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone, Copy)]
#[tsify(into_wasm_abi, from_wasm_abi)]
#[serde(rename_all = "snake_case")]
pub enum AddressClass {
    Private,
    Mesh,
    Hostname,
    PublicIp,
    LoopbackOrWildcard,
}

impl From<AddressClassImpl> for AddressClass {
    fn from(c: AddressClassImpl) -> Self {
        match c {
            AddressClassImpl::Private => AddressClass::Private,
            AddressClassImpl::Mesh => AddressClass::Mesh,
            AddressClassImpl::Hostname => AddressClass::Hostname,
            AddressClassImpl::PublicIp => AddressClass::PublicIp,
            AddressClassImpl::LoopbackOrWildcard => AddressClass::LoopbackOrWildcard,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteCandidate {
    pub key: String,
    pub kind: RouteKind,
    pub priority: i32,
    pub probe_host: String,
}

impl From<RouteCandidate> for RouteCandidateImpl {
    fn from(c: RouteCandidate) -> Self {
        RouteCandidateImpl {
            key: c.key,
            kind: c.kind.into(),
            priority: c.priority,
            probe_host: c.probe_host,
        }
    }
}

impl From<RouteCandidateImpl> for RouteCandidate {
    fn from(c: RouteCandidateImpl) -> Self {
        RouteCandidate {
            key: c.key,
            kind: c.kind.into(),
            priority: c.priority,
            probe_host: c.probe_host,
        }
    }
}

/// wasm-bindgen cannot pass a bare `Vec<T>` of tsify structs across the
/// boundary, so collections travel in a named wrapper.
#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteCandidateList {
    pub items: Vec<RouteCandidate>,
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteEntryList {
    pub items: Vec<RouteEntry>,
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct PickerConfig {
    pub quarantine_ms: f64,
    pub cooldown_ms: f64,
    pub explore_every: f64,
    pub ewma_weight: f64,
    pub task_ewma_half_life_ms: f64,
    pub explore_band: f64,
    pub task_ewma_alpha: f64,
    pub transport_quarantine_threshold: f64,
}

impl From<PickerConfig> for PickerConfigImpl {
    fn from(c: PickerConfig) -> Self {
        PickerConfigImpl {
            quarantine_ms: c.quarantine_ms as i64,
            cooldown_ms: c.cooldown_ms as i64,
            explore_every: c.explore_every.max(0.0) as u64,
            ewma_weight: c.ewma_weight,
            task_ewma_half_life_ms: c.task_ewma_half_life_ms as i64,
            explore_band: c.explore_band,
            task_ewma_alpha: c.task_ewma_alpha,
            transport_quarantine_threshold: c.transport_quarantine_threshold.max(0.0) as u32,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteObservation {
    pub key: String,
    pub kind: RouteKind,
    pub priority: i32,
    pub probe_host: String,
    pub is_lan: bool,
    pub state: RouteState,
    pub score: Option<f64>,
    pub rtt_ms: Option<f64>,
    pub task_ewma_ms: Option<f64>,
    pub last_success_ms: Option<f64>,
    pub quarantine_until_ms: Option<f64>,
    pub cooldown_until_ms: Option<f64>,
    pub consecutive_failures: u32,
    pub consecutive_transport_failures: u32,
    pub sticky_quarantine: bool,
}

impl From<RouteObservationImpl> for RouteObservation {
    fn from(o: RouteObservationImpl) -> Self {
        RouteObservation {
            key: o.key,
            kind: o.kind.into(),
            priority: o.priority,
            probe_host: o.probe_host,
            is_lan: o.is_lan,
            state: o.state.into(),
            score: o.score,
            rtt_ms: o.rtt_ms.map(|v| v as f64),
            task_ewma_ms: o.task_ewma_ms,
            last_success_ms: o.last_success_ms.map(|v| v as f64),
            quarantine_until_ms: o.quarantine_until_ms.map(|v| v as f64),
            cooldown_until_ms: o.cooldown_until_ms.map(|v| v as f64),
            consecutive_failures: o.consecutive_failures,
            consecutive_transport_failures: o.consecutive_transport_failures,
            sticky_quarantine: o.sticky_quarantine,
        }
    }
}

/// The topology read model: what is selected, the ranked order, and the evidence
/// behind each position. Emit on change and a UI can draw a live path — which
/// node, which route class, measured RTT — and animate failover, without
/// reimplementing any ranking logic.
#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteSnapshot {
    pub selected: Option<String>,
    pub routes: Vec<RouteObservation>,
    pub pick_count: f64,
}

impl From<RouteSnapshotImpl> for RouteSnapshot {
    fn from(s: RouteSnapshotImpl) -> Self {
        RouteSnapshot {
            selected: s.selected,
            routes: s.routes.into_iter().map(Into::into).collect(),
            pick_count: s.pick_count as f64,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct RouteEntry {
    pub key: String,
    pub target_id: String,
    pub label: String,
    pub route: String,
    pub kind: RouteKind,
    pub priority: i32,
    pub relay_id: Option<String>,
    pub urls: BTreeMap<String, String>,
}

impl From<RouteEntryImpl> for RouteEntry {
    fn from(e: RouteEntryImpl) -> Self {
        RouteEntry {
            key: e.key,
            target_id: e.target_id,
            label: e.label,
            route: e.route,
            kind: e.kind.into(),
            priority: e.priority,
            relay_id: e.relay_id,
            urls: e.urls,
        }
    }
}

impl From<RouteEntry> for RouteEntryImpl {
    fn from(e: RouteEntry) -> Self {
        RouteEntryImpl {
            key: e.key,
            target_id: e.target_id,
            label: e.label,
            route: e.route,
            kind: e.kind.into(),
            priority: e.priority,
            relay_id: e.relay_id,
            urls: e.urls,
        }
    }
}

#[derive(Tsify, Serialize, Deserialize, Clone)]
#[tsify(into_wasm_abi, from_wasm_abi)]
pub struct DirectEndpoint {
    pub uri: String,
    pub host: String,
    pub port: u32,
    pub address_class: AddressClass,
}

// ---------------------------------------------------------------------------
// Picker
// ---------------------------------------------------------------------------

/// Selection state machine. Construct one per logical target, feed it the
/// current candidate set, drive it with probe/outcome feedback, and ask it for
/// `pickBest` / `ranked` / `snapshot`.
///
/// All times are epoch milliseconds supplied by the caller (`Date.now()`): the
/// engine reads no clock, so behaviour is deterministic and testable.
#[wasm_bindgen]
pub struct RoutePicker {
    inner: PickerImpl,
}

fn to_exclude(exclude: Option<Vec<String>>) -> BTreeSet<String> {
    exclude.unwrap_or_default().into_iter().collect()
}

#[wasm_bindgen]
impl RoutePicker {
    #[wasm_bindgen(constructor)]
    pub fn new(config: Option<PickerConfig>) -> RoutePicker {
        RoutePicker {
            inner: match config {
                Some(c) => PickerImpl::new(c.into()),
                None => PickerImpl::default(),
            },
        }
    }

    /// Replace the candidate set. Stats for keys still present are preserved;
    /// stats for departed keys are dropped.
    pub fn setCandidates(&mut self, candidates: RouteCandidateList) {
        self.inner
            .set_candidates(candidates.items.into_iter().map(Into::into).collect());
    }

    /// The current best route key, or `undefined` when there are no candidates.
    /// Advances the exploration counter.
    pub fn pickBest(&mut self, nowMs: f64, exclude: Option<Vec<String>>) -> Option<String> {
        self.inner
            .pick_best(nowMs as i64, &to_exclude(exclude))
            .map(|c| c.key)
    }

    /// Candidate keys best-first. Does not advance the exploration counter.
    pub fn ranked(&mut self, nowMs: f64, exclude: Option<Vec<String>>) -> Vec<String> {
        self.inner
            .ranked(nowMs as i64, &to_exclude(exclude))
            .into_iter()
            .map(|c| c.key)
            .collect()
    }

    /// A reachability probe succeeded with round-trip `rttMs`.
    pub fn recordReachable(&mut self, key: &str, rttMs: f64, nowMs: f64) {
        self.inner.record_reachable(key, rttMs as i64, nowMs as i64);
    }

    /// A reachability probe failed. Two in a row ⇒ quarantine.
    pub fn recordUnreachable(&mut self, key: &str, nowMs: f64) {
        self.inner.record_unreachable(key, nowMs as i64);
    }

    /// A completed task's responsiveness (first-byte ms) — the dominant signal.
    pub fn recordTaskLatency(&mut self, key: &str, ms: f64, nowMs: f64) {
        self.inner.record_task_latency(key, ms as i64, nowMs as i64);
    }

    /// A transport-level failure (refused/dropped/no-output). Arms the cooldown.
    pub fn recordTransportFailure(&mut self, key: &str, nowMs: f64) {
        self.inner.record_transport_failure(key, nowMs as i64);
    }

    /// Accepted the request then failed mid-use. Quarantines on one strike.
    pub fn recordPathFailure(&mut self, key: &str, nowMs: f64) {
        self.inner.record_path_failure(key, nowMs as i64);
    }

    /// A capability/auth failure. Deliberately does not penalize reachability —
    /// the transport was never attempted.
    pub fn recordAuthFailure(&mut self, key: &str) {
        self.inner.record_auth_failure(key);
    }

    /// Any successful use clears quarantine and cooldown.
    pub fn recordSuccess(&mut self, key: &str, nowMs: f64) {
        self.inner.record_success(key, nowMs as i64);
    }

    /// Drop every negative gate so all routes are re-challenged. For a network
    /// transition (cellular → wifi/mesh); learned latency is kept.
    pub fn resetQuarantines(&mut self) {
        self.inner.reset_quarantines();
    }

    pub fn rttMs(&self, key: &str) -> Option<f64> {
        self.inner.rtt_ms(key).map(|v| v as f64)
    }

    pub fn taskEwmaMs(&self, key: &str) -> Option<f64> {
        self.inner.task_ewma_ms(key)
    }

    pub fn isQuarantined(&mut self, key: &str, nowMs: f64) -> bool {
        self.inner.is_quarantined(key, nowMs as i64)
    }

    pub fn isCooling(&self, key: &str, nowMs: f64) -> bool {
        self.inner.is_cooling(key, nowMs as i64)
    }

    /// Full read model for telemetry / topology UIs. Observation only — polling
    /// it cannot perturb selection.
    pub fn snapshot(&mut self, nowMs: f64) -> RouteSnapshot {
        self.inner.snapshot(nowMs as i64).into()
    }
}

// ---------------------------------------------------------------------------
// Manifest + host helpers
// ---------------------------------------------------------------------------

/// Parse the `targetsKey` array of a route manifest into flat per-route entries,
/// sorted by priority. Any string field ending in `_url` is collected, so a
/// producer can add a transport without a schema change here.
#[wasm_bindgen]
pub fn parseRouteManifest(
    manifest: JsValue,
    targetsKey: &str,
    defaultTargetId: &str,
) -> Result<RouteEntryList, JsValue> {
    let value: serde_json::Value = serde_wasm_bindgen::from_value(manifest)
        .map_err(|e| JsValue::from_str(&format!("invalid manifest: {e}")))?;
    Ok(RouteEntryList {
        items: parse_route_manifest_impl(&value, targetsKey, defaultTargetId)
            .into_iter()
            .map(Into::into)
            .collect(),
    })
}

/// Build picker candidates from parsed entries. `probeField` names the URL field
/// whose host the caller's reachability probe targets (e.g. `health_url`).
#[wasm_bindgen]
pub fn candidatesFromEntries(entries: RouteEntryList, probeField: &str) -> RouteCandidateList {
    let impls: Vec<RouteEntryImpl> = entries.items.into_iter().map(Into::into).collect();
    RouteCandidateList {
        items: candidates_from_entries_impl(&impls, probeField)
            .into_iter()
            .map(Into::into)
            .collect(),
    }
}

/// Validate an endpoint URL. `allowedSchemes` empty ⇒ any scheme. Throws with a
/// human-readable reason when the URL is rejected.
#[wasm_bindgen]
pub fn parseDirectEndpoint(
    raw: Option<String>,
    allowedSchemes: Option<Vec<String>>,
    allowLoopback: Option<bool>,
    rejectPublicIp: Option<bool>,
) -> Result<DirectEndpoint, JsValue> {
    let policy = EndpointPolicyImpl {
        allowed_schemes: allowedSchemes
            .unwrap_or_else(|| vec!["ws".to_string(), "wss".to_string()])
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect(),
        allow_loopback: allowLoopback.unwrap_or(false),
        reject_public_ip: rejectPublicIp.unwrap_or(true),
    };
    parse_direct_endpoint_impl(raw.as_deref(), &policy)
        .map(|ep| DirectEndpoint {
            uri: ep.uri,
            host: ep.host,
            port: ep.port,
            address_class: ep.address_class.into(),
        })
        .map_err(|reason| JsValue::from_str(&reason))
}

#[wasm_bindgen]
pub fn routeKindFromWire(route: &str) -> RouteKind {
    RouteKindImpl::from_wire(route).into()
}

#[wasm_bindgen]
pub fn hostOf(hostOrUrl: &str) -> String {
    host_of_impl(hostOrUrl)
}

/// RFC1918 only. Never present this to a user as "same LAN" — an RFC1918 address
/// can arrive over a VPN/VPC/overlay; it is a probe-priority hint, not proof of
/// locality.
#[wasm_bindgen]
pub fn isLanHost(hostOrUrl: &str) -> bool {
    is_lan_host_impl(hostOrUrl)
}

/// CGNAT 100.64/10 — the tunnel/overlay class.
#[wasm_bindgen]
pub fn isMeshHost(hostOrUrl: &str) -> bool {
    is_mesh_host_impl(hostOrUrl)
}

#[wasm_bindgen]
pub fn isPrivateHost(hostOrUrl: &str) -> bool {
    is_private_host_impl(hostOrUrl)
}

#[wasm_bindgen]
pub fn classifyDirectHost(hostOrUrl: &str) -> AddressClass {
    classify_direct_host(hostOrUrl).into()
}
