//! Route selection core — the pure engine behind client → endpoint path choice.
//!
//! This is the **functional core**: state in, ranking out. It owns per-candidate
//! health, learned task-latency (EWMA with age decay), quarantine, transport
//! cooldown, exploration, failover exclusion, and the pessimistic bootstrap that
//! stops an unmeasured node from dethroning a proven one. It performs **no I/O**
//! — no sockets, no health probes, no DNS, no clock. The host performs the
//! probing and reports results back through the `record_*` hooks; time is passed
//! in as `now_ms` (epoch millis) so the core stays pure and deterministically
//! testable. Same doctrine as [`crate::residency`].
//!
//! **Transport-agnostic by construction.** Nothing here knows about WebSockets,
//! HTTP, gRPC or QUIC. A candidate is `(key, kind, priority, probe_host)`; how
//! you reach it and how you measure it are the host's business. The engine only
//! answers "which path should I use next, and why".
//!
//! Two distinct lanes must not be confused (see benchday `docs/mesh-route.md`):
//! this is **client → endpoint selection** (a client choosing the fastest
//! physical path to a named service). It is *not* **hub → worker placement**
//! (which GPU host runs a job) — that is the `ExecutionBackend` / capability
//! lease lane. They may both be livestack capabilities; they do not share a
//! scoring model.
//!
//! Ported from benchday `packages/mesh_route` (pure Dart, io-injected), which
//! collapsed three forked selectors — daemon route, narration TTS, and ASR — into
//! one engine. This crate is the single implementation that Dart, TS and Python
//! bind to, so the three cannot re-fork across languages.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Address classification
// ---------------------------------------------------------------------------

/// Extract the host from either a bare host (`192.168.1.5`) or a full URL
/// (`ws://192.168.1.5:7778/`). Deliberately dependency-free and lenient: a
/// string with no `://` is returned as-is, matching the reference behaviour of
/// `Uri.tryParse(...).host` falling back to the raw value.
pub fn host_of(host_or_url: &str) -> String {
    let raw = host_or_url.trim();
    let after_scheme = match raw.find("://") {
        Some(i) => &raw[i + 3..],
        // No authority component: the caller handed us a bare host.
        None => return raw.to_string(),
    };
    // Authority ends at the first path/query/fragment delimiter.
    let authority = after_scheme
        .find(['/', '?', '#'])
        .map_or(after_scheme, |i| &after_scheme[..i]);
    // Strip userinfo.
    let hostport = authority.rfind('@').map_or(authority, |i| &authority[i + 1..]);
    // An IPv6 literal is bracketed; the brackets are not part of the host.
    if let Some(close) = hostport.find(']') {
        if hostport.starts_with('[') {
            return hostport[1..close].to_string();
        }
    }
    hostport
        .rfind(':')
        .map_or(hostport, |i| &hostport[..i])
        .to_string()
}

fn ipv4_octets(host: &str) -> Option<[u16; 4]> {
    let mut out = [0u16; 4];
    let mut n = 0;
    for part in host.split('.') {
        if n == 4 {
            return None;
        }
        // Reject empty and non-numeric segments; `parse` also rejects signs.
        let v: u16 = part.parse().ok()?;
        if v > 255 {
            return None;
        }
        out[n] = v;
        n += 1;
    }
    if n == 4 {
        Some(out)
    } else {
        None
    }
}

/// True for an RFC1918 host (10/8, 192.168/16, 172.16–31). Accepts a bare host
/// or a full URL.
///
/// The CGNAT mesh range (100.64/10) is intentionally **not** included — it rides
/// the tunnel and gets the full warm-up probe budget.
///
/// NOTE: "private" is the accurate concept ([`is_private_host`]). RFC1918 syntax
/// is a probe-priority hint, **not** proof of same-network locality — an RFC1918
/// address can arrive over a VPN/VPC/overlay — so it must never be presented to
/// a user as "same LAN".
pub fn is_lan_host(host_or_url: &str) -> bool {
    match ipv4_octets(&host_of(host_or_url)) {
        Some([10, _, _, _]) => true,
        Some([192, 168, _, _]) => true,
        Some([172, b, _, _]) => (16..=31).contains(&b),
        _ => false,
    }
}

/// True for the CGNAT mesh range (100.64.0.0/10) — the tunnel/overlay class.
/// Not LAN (see [`is_lan_host`]).
pub fn is_mesh_host(host_or_url: &str) -> bool {
    matches!(ipv4_octets(&host_of(host_or_url)), Some([100, b, _, _]) if (64..=127).contains(&b))
}

/// True for a private-range host a direct route may use: RFC1918 or CGNAT mesh.
pub fn is_private_host(host_or_url: &str) -> bool {
    is_lan_host(host_or_url) || is_mesh_host(host_or_url)
}

/// True for a dotted-quad IPv4 literal.
pub fn is_ipv4_literal(host: &str) -> bool {
    ipv4_octets(host).is_some()
}

/// Address class of a direct-endpoint host, used for validation + probe budget.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AddressClass {
    /// RFC1918 LAN — the fast co-located path; gets the short probe budget.
    Private,
    /// CGNAT 100.64/10 mesh/overlay — tunnel-tolerant probe budget.
    Mesh,
    /// A DNS name (only ever from explicit config; cannot be classified without
    /// resolving) — tunnel-tolerant budget, allowed.
    Hostname,
    /// A routable public IP literal — rejected for direct routes by default.
    PublicIp,
    /// Loopback or wildcard — rejected unless explicitly allowed.
    LoopbackOrWildcard,
}

/// Bucket a host by address class.
pub fn classify_direct_host(host_or_url: &str) -> AddressClass {
    let host = host_of(host_or_url).to_lowercase();
    if host.is_empty()
        || host == "localhost"
        || host == "127.0.0.1"
        || host == "::1"
        || host == "0.0.0.0"
        || host == "::"
    {
        return AddressClass::LoopbackOrWildcard;
    }
    if is_lan_host(&host) {
        return AddressClass::Private;
    }
    if is_mesh_host(&host) {
        return AddressClass::Mesh;
    }
    if is_ipv4_literal(&host) {
        return AddressClass::PublicIp;
    }
    // Non-IPv4 literal: a DNS name or an IPv6 literal we don't classify.
    AddressClass::Hostname
}

// ---------------------------------------------------------------------------
// Endpoint validation
// ---------------------------------------------------------------------------

/// What [`parse_direct_endpoint`] will accept. The reference implementation
/// hardcoded `ws`/`wss`; making the scheme set a parameter is what turns this
/// from a WebSocket gate into a transport-agnostic one.
#[derive(Clone, Debug)]
pub struct EndpointPolicy {
    /// Lowercase schemes that may enter the probe ladder.
    pub allowed_schemes: Vec<String>,
    /// Relax only the loopback/wildcard rejection. Probes dial a loopback server
    /// in unit tests as a private-IP stand-in; production candidates never are.
    pub allow_loopback: bool,
    /// Reject routable public IP literals for a "direct" route.
    pub reject_public_ip: bool,
}

impl Default for EndpointPolicy {
    fn default() -> Self {
        EndpointPolicy {
            allowed_schemes: vec!["ws".to_string(), "wss".to_string()],
            allow_loopback: false,
            reject_public_ip: true,
        }
    }
}

impl EndpointPolicy {
    /// Policy for plain HTTP(S) endpoints — health URLs, batch APIs.
    pub fn http() -> Self {
        EndpointPolicy {
            allowed_schemes: vec!["http".to_string(), "https".to_string()],
            ..EndpointPolicy::default()
        }
    }

    /// Accept any scheme; still applies the host-class rules.
    pub fn any_scheme() -> Self {
        EndpointPolicy {
            allowed_schemes: Vec::new(),
            ..EndpointPolicy::default()
        }
    }
}

/// The well-known default port for a scheme, when the URL omits one.
fn default_port_for(scheme: &str) -> Option<u32> {
    match scheme {
        "ws" | "http" => Some(80),
        "wss" | "https" => Some(443),
        _ => None,
    }
}

/// A strictly-validated direct endpoint.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DirectEndpoint {
    /// Canonical `scheme://host[:port]/` form — stable for cache keys/compares.
    /// The port is omitted when it is the scheme default.
    pub uri: String,
    pub host: String,
    pub port: u32,
    pub address_class: AddressClass,
}

impl DirectEndpoint {
    /// `host:port` label for telemetry / probe results.
    pub fn target(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
    pub fn is_private(&self) -> bool {
        self.address_class == AddressClass::Private
    }
    pub fn is_mesh(&self) -> bool {
        self.address_class == AddressClass::Mesh
    }
}

/// Strictly validate a configured or hub-advertised endpoint URL. This is the
/// single gate every direct URL passes before entering the probe ladder, so
/// configured and hub-forwarded URLs are held to identical rules — hub-forwarded
/// URLs are treated as untrusted serialized input even though an authenticated
/// peer originated them.
///
/// Returns the endpoint or a human-readable reason for rejection.
pub fn parse_direct_endpoint(
    raw: Option<&str>,
    policy: &EndpointPolicy,
) -> Result<DirectEndpoint, String> {
    let value = raw.unwrap_or("").trim();
    if value.is_empty() {
        return Err("empty URL".to_string());
    }
    let scheme_end = value.find("://").ok_or_else(|| "unparseable URL".to_string())?;
    let scheme = value[..scheme_end].to_lowercase();
    if scheme.is_empty() {
        return Err("unparseable URL".to_string());
    }
    if !policy.allowed_schemes.is_empty() && !policy.allowed_schemes.contains(&scheme) {
        return Err(format!(
            "scheme must be one of {} (got \"{}\")",
            policy.allowed_schemes.join("/"),
            scheme
        ));
    }
    let host = host_of(value);
    let host = host.trim().to_string();
    if host.is_empty() {
        return Err("missing host".to_string());
    }
    let cls = classify_direct_host(&host);
    match cls {
        AddressClass::LoopbackOrWildcard if !policy.allow_loopback => {
            return Err("loopback/wildcard host rejected".to_string());
        }
        AddressClass::PublicIp if policy.reject_public_ip => {
            return Err("public IP rejected for direct route".to_string());
        }
        _ => {}
    }
    let port = match explicit_port(value) {
        Some(Ok(p)) => p,
        Some(Err(reason)) => return Err(reason),
        None => default_port_for(&scheme)
            .ok_or_else(|| format!("port required for scheme \"{scheme}\""))?,
    };
    if port == 0 || port > 65535 {
        return Err(format!("invalid port {port}"));
    }
    let uri = if default_port_for(&scheme) == Some(port) {
        format!("{scheme}://{host}/")
    } else {
        format!("{scheme}://{host}:{port}/")
    };
    Ok(DirectEndpoint {
        uri,
        host,
        port,
        address_class: cls,
    })
}

/// The explicit port in a URL's authority, if present. `Some(Err)` means a port
/// was written but is not a number.
fn explicit_port(url: &str) -> Option<Result<u32, String>> {
    let after_scheme = &url[url.find("://")? + 3..];
    let authority = after_scheme
        .find(['/', '?', '#'])
        .map_or(after_scheme, |i| &after_scheme[..i]);
    let hostport = authority.rfind('@').map_or(authority, |i| &authority[i + 1..]);
    // Skip an IPv6 literal's internal colons.
    let search_from = hostport.find(']').map_or(0, |i| i + 1);
    let colon = hostport[search_from..].find(':')? + search_from;
    let port_str = &hostport[colon + 1..];
    if port_str.is_empty() {
        return Some(Err("invalid port".to_string()));
    }
    Some(
        port_str
            .parse::<u32>()
            .map_err(|_| format!("invalid port {port_str}")),
    )
}

// ---------------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------------

/// Physical route class to a target. Mirrors the `route` field a control plane
/// advertises per candidate.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteKind {
    Direct,
    RegionalRelay,
    HubProxy,
    WebRtcP2p,
}

impl RouteKind {
    /// Parse an advertised `route` string. Centralizes the string-match that
    /// tends to get triplicated across call sites. Unknown ⇒ [`RouteKind::Direct`].
    pub fn from_wire(route: &str) -> RouteKind {
        match route.trim() {
            "regional_relay" => RouteKind::RegionalRelay,
            "hub_proxy" => RouteKind::HubProxy,
            "webrtc_p2p" => RouteKind::WebRtcP2p,
            _ => RouteKind::Direct,
        }
    }

    pub fn to_wire(self) -> &'static str {
        match self {
            RouteKind::Direct => "direct",
            RouteKind::RegionalRelay => "regional_relay",
            RouteKind::HubProxy => "hub_proxy",
            RouteKind::WebRtcP2p => "webrtc_p2p",
        }
    }
}

/// One candidate path to a target.
///
/// `key` is the stable identity the picker uses to carry learned stats across
/// candidate-set rebuilds. The engine holds **no caller payload**: bind your own
/// connection object to the key on your side. That is what keeps this FFI-clean
/// across Dart, TS and Python.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteCandidate {
    pub key: String,
    pub kind: RouteKind,
    /// Server-advertised preference; lower wins. Final tie-break only.
    pub priority: i32,
    /// The host the caller's probe targets — used to classify [`RouteCandidate::is_lan`].
    pub probe_host: String,
}

impl RouteCandidate {
    pub fn new(key: impl Into<String>, kind: RouteKind, probe_host: impl Into<String>) -> Self {
        RouteCandidate {
            key: key.into(),
            kind,
            priority: 100,
            probe_host: probe_host.into(),
        }
    }

    pub fn with_priority(mut self, priority: i32) -> Self {
        self.priority = priority;
        self
    }

    /// True for an RFC1918 LAN host — short probe budget + score-tie win.
    pub fn is_lan(&self) -> bool {
        is_lan_host(&self.probe_host)
    }

    /// A direct LAN/mesh route is preferred over a relay/hub-proxy in tie-breaks.
    pub fn prefers_in_tie(&self) -> bool {
        matches!(self.kind, RouteKind::Direct | RouteKind::WebRtcP2p)
    }
}

// ---------------------------------------------------------------------------
// Picker
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default)]
struct RouteStat {
    last_rtt_ms: Option<i64>,
    task_ewma_ms: Option<f64>,
    /// When `task_ewma_ms` was last updated — used to decay a stale EWMA's
    /// weight toward fresh RTT so one slow boot sample doesn't haunt a node.
    task_ewma_at: Option<i64>,
    last_success: Option<i64>,
    quarantine_until: Option<i64>,
    cooldown_until: Option<i64>,
    consecutive_failures: u32,
    consecutive_transport_failures: u32,
    /// When true, a reachability-probe success does **not** clear
    /// `quarantine_until` — only a real task success or expiry does. Armed by a
    /// task-path failure, where `/health` can answer fine while the task path is
    /// broken (the dictation-stall shape: stream stalled, health 200).
    sticky_quarantine: bool,
}

/// Tunables. Defaults match the production reference implementation.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PickerConfig {
    pub quarantine_ms: i64,
    pub cooldown_ms: i64,
    /// Every Nth pick swaps the top two eligible so the runner-up's metric stays
    /// fresh. `0` disables exploration.
    pub explore_every: u64,
    /// Weight of learned task latency when blended with fresh RTT.
    pub ewma_weight: f64,
    /// Half-life over which a task EWMA's weight decays toward fresh RTT.
    pub task_ewma_half_life_ms: i64,
    /// Exploration only re-challenges the favorite when the runner-up's score is
    /// within this multiple of the leader's. Routing a real request to a node
    /// already known to be much slower just pays its latency for nothing.
    pub explore_band: f64,
    /// Smoothing factor for a new task-latency sample.
    pub task_ewma_alpha: f64,
    /// Consecutive transport failures that escalate a route from cooldown to a
    /// full sticky quarantine — so a reachable-but-broken node (health 200, task
    /// always fails) stops resurfacing after every cooldown lapse.
    pub transport_quarantine_threshold: u32,
}

impl Default for PickerConfig {
    fn default() -> Self {
        PickerConfig {
            quarantine_ms: 60_000,
            cooldown_ms: 10_000,
            explore_every: 5,
            ewma_weight: 0.6,
            task_ewma_half_life_ms: 15 * 60_000,
            explore_band: 1.5,
            task_ewma_alpha: 0.3,
            transport_quarantine_threshold: 3,
        }
    }
}

/// Why a route is not currently eligible.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteState {
    Eligible,
    /// Transport failure cooldown — sinks without permanent exclusion.
    Cooling,
    /// Quarantined after repeated failures.
    Quarantined,
}

/// Per-route observation for telemetry and topology UIs. This is the read model
/// a "which server am I on, and how did I get there" view renders.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RouteObservation {
    pub key: String,
    pub kind: RouteKind,
    pub priority: i32,
    pub probe_host: String,
    pub is_lan: bool,
    pub state: RouteState,
    /// Effective comparison score (lower = better); `None` when unmeasured.
    pub score: Option<f64>,
    pub rtt_ms: Option<i64>,
    pub task_ewma_ms: Option<f64>,
    pub last_success_ms: Option<i64>,
    pub quarantine_until_ms: Option<i64>,
    pub cooldown_until_ms: Option<i64>,
    pub consecutive_failures: u32,
    pub consecutive_transport_failures: u32,
    pub sticky_quarantine: bool,
}

/// A point-in-time view of the whole selection state: what would be chosen now,
/// in what order, and the evidence behind each position.
///
/// This is the observability half of the capability — emit it on change and a
/// client can draw its live path (which node, which route class, measured RTT)
/// and animate failover, without the UI reimplementing any ranking logic.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RouteSnapshot {
    /// Key of the route [`Picker::pick_best`] would return right now.
    pub selected: Option<String>,
    /// Best-first, same order [`Picker::ranked`] returns.
    pub routes: Vec<RouteObservation>,
    pub pick_count: u64,
}

/// Selection state machine. Construct once per logical target, feed it the
/// current candidate set, drive it with probe/outcome feedback, and ask it for
/// [`Picker::pick_best`] / [`Picker::ranked`].
#[derive(Clone, Debug)]
pub struct Picker {
    config: PickerConfig,
    stats: BTreeMap<String, RouteStat>,
    candidates: Vec<RouteCandidate>,
    pick_count: u64,
}

impl Default for Picker {
    fn default() -> Self {
        Picker::new(PickerConfig::default())
    }
}

impl Picker {
    pub fn new(config: PickerConfig) -> Self {
        Picker {
            config,
            stats: BTreeMap::new(),
            candidates: Vec::new(),
            pick_count: 0,
        }
    }

    pub fn config(&self) -> &PickerConfig {
        &self.config
    }

    /// Replace the candidate set. Stats for keys still present are preserved;
    /// stats for departed keys are dropped.
    pub fn set_candidates(&mut self, candidates: Vec<RouteCandidate>) {
        let live: BTreeSet<String> = candidates.iter().map(|c| c.key.clone()).collect();
        self.stats.retain(|k, _| live.contains(k));
        for c in &candidates {
            self.stats.entry(c.key.clone()).or_default();
        }
        self.candidates = candidates;
    }

    pub fn candidates(&self) -> &[RouteCandidate] {
        &self.candidates
    }

    pub fn pick_count(&self) -> u64 {
        self.pick_count
    }

    fn quarantined(stat: &mut RouteStat, now_ms: i64) -> bool {
        match stat.quarantine_until {
            Some(until) if now_ms < until => true,
            Some(_) => {
                // Expired — clear the cause flag so a later probe success behaves
                // normally.
                stat.sticky_quarantine = false;
                false
            }
            None => false,
        }
    }

    fn cooling(stat: &RouteStat, now_ms: i64) -> bool {
        matches!(stat.cooldown_until, Some(until) if now_ms < until)
    }

    /// Lower is better. Learned task latency dominates (it reflects real
    /// responsiveness a health ping can't), blended with fresh RTT so a stale
    /// favorite that slows down is re-challenged. Never-measured ⇒ +inf.
    fn score(&self, stat: &RouteStat, now_ms: i64) -> f64 {
        let rtt = stat.last_rtt_ms.map(|v| v as f64);
        match (stat.task_ewma_ms, rtt) {
            (Some(ewma), Some(rtt)) => {
                let w = self.decayed_ewma_weight(stat, now_ms);
                ewma * w + rtt * (1.0 - w)
            }
            (Some(ewma), None) => ewma,
            (None, Some(rtt)) => rtt,
            (None, None) => f64::INFINITY,
        }
    }

    /// `ewma_weight` decayed by the EWMA's age. As the sample ages the weight
    /// tends to 0, so [`Picker::score`] tends to the fresh RTT.
    fn decayed_ewma_weight(&self, stat: &RouteStat, now_ms: i64) -> f64 {
        let Some(at) = stat.task_ewma_at else {
            return self.config.ewma_weight;
        };
        let age_ms = now_ms - at;
        if age_ms <= 0 || self.config.task_ewma_half_life_ms <= 0 {
            return self.config.ewma_weight;
        }
        let half_lives = age_ms as f64 / self.config.task_ewma_half_life_ms as f64;
        self.config.ewma_weight * 0.5f64.powf(half_lives)
    }

    /// Effective comparison score per candidate (lower = better), over the given
    /// candidate set so sibling context is available.
    ///
    /// A candidate with only a probe RTT (no task EWMA) is **bootstrapped
    /// pessimistically** to the best measured sibling's score — so a freshly
    /// advertised node can't dethrone a proven-fast one on raw RTT alone (its
    /// bare ~20 ms ping vs the proven node's hundreds-of-ms blended score) before
    /// it has served a single request. Cold start (no node has an EWMA yet)
    /// falls back to plain RTT ranking.
    fn effective_scores(&self, cands: &[&RouteCandidate], now_ms: i64) -> BTreeMap<String, f64> {
        let mut best_measured: Option<f64> = None;
        for c in cands {
            if let Some(stat) = self.stats.get(&c.key) {
                if stat.task_ewma_ms.is_some() {
                    let sc = self.score(stat, now_ms);
                    if best_measured.is_none_or(|b| sc < b) {
                        best_measured = Some(sc);
                    }
                }
            }
        }
        let mut out = BTreeMap::new();
        for c in cands {
            let Some(stat) = self.stats.get(&c.key) else {
                out.insert(c.key.clone(), f64::INFINITY);
                continue;
            };
            if stat.task_ewma_ms.is_some() {
                out.insert(c.key.clone(), self.score(stat, now_ms));
                continue;
            }
            let value = match (stat.last_rtt_ms, best_measured) {
                (None, _) => f64::INFINITY,
                (Some(rtt), Some(best)) => {
                    let rtt = rtt as f64;
                    if rtt > best {
                        rtt
                    } else {
                        best
                    }
                }
                (Some(rtt), None) => rtt as f64,
            };
            out.insert(c.key.clone(), value);
        }
        out
    }

    fn compare_with(
        &self,
        scores: &BTreeMap<String, f64>,
        a: &RouteCandidate,
        b: &RouteCandidate,
    ) -> Ordering {
        let sa = scores.get(&a.key).copied().unwrap_or(f64::INFINITY);
        let sb = scores.get(&b.key).copied().unwrap_or(f64::INFINITY);
        let by_score = sa.partial_cmp(&sb).unwrap_or(Ordering::Equal);
        if by_score != Ordering::Equal {
            return by_score;
        }
        // A real measurement beats an equally-scored pessimistic bootstrap, so
        // the proven node stays ahead of the unmeasured one at a score tie.
        let am = self
            .stats
            .get(&a.key)
            .is_some_and(|s| s.task_ewma_ms.is_some());
        let bm = self
            .stats
            .get(&b.key)
            .is_some_and(|s| s.task_ewma_ms.is_some());
        if am != bm {
            return if am { Ordering::Less } else { Ordering::Greater };
        }
        if a.is_lan() != b.is_lan() {
            return if a.is_lan() {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        if a.prefers_in_tie() != b.prefers_in_tie() {
            return if a.prefers_in_tie() {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        a.priority.cmp(&b.priority)
    }

    /// Candidates best-first. Eligible (not quarantined, not cooling) come first,
    /// internally score-sorted; gated candidates follow as a least-bad tail so a
    /// pick is always possible when any candidate exists. Every
    /// `explore_every`-th call swaps the top two eligible so the runner-up's
    /// metric stays fresh.
    ///
    /// `exclude` is a one-shot hard exclusion (by key) for failover — the caller
    /// dropping the just-failed route before its quarantine/cooldown has armed.
    ///
    /// Sorting is **stable**, so equal-ranked candidates keep the order they were
    /// supplied in — the ranking is fully deterministic for a given input.
    pub fn ranked(&mut self, now_ms: i64, exclude: &BTreeSet<String>) -> Vec<RouteCandidate> {
        let mut eligible: Vec<RouteCandidate> = Vec::new();
        let mut gated: Vec<RouteCandidate> = Vec::new();
        for c in &self.candidates {
            if exclude.contains(&c.key) {
                continue;
            }
            let Some(stat) = self.stats.get_mut(&c.key) else {
                continue;
            };
            if Self::quarantined(stat, now_ms) || Self::cooling(stat, now_ms) {
                gated.push(c.clone());
            } else {
                eligible.push(c.clone());
            }
        }

        let all: Vec<&RouteCandidate> = eligible.iter().chain(gated.iter()).collect();
        let scores = self.effective_scores(&all, now_ms);

        eligible.sort_by(|a, b| self.compare_with(&scores, a, b));
        gated.sort_by(|a, b| self.compare_with(&scores, a, b));

        if self.config.explore_every > 0
            && eligible.len() >= 2
            && self.pick_count > 0
            && self.pick_count.is_multiple_of(self.config.explore_every)
        {
            let lead = scores.get(&eligible[0].key).copied().unwrap_or(f64::INFINITY);
            let runner_up = scores.get(&eligible[1].key).copied().unwrap_or(f64::INFINITY);
            // Only re-challenge the favorite when the runner-up is plausibly
            // competitive. A bootstrapped (unmeasured) runner-up ties the leader,
            // so it's still explored — that's how it gets measured.
            if runner_up.is_finite() && runner_up <= lead * self.config.explore_band {
                eligible.swap(0, 1);
            }
        }

        eligible.extend(gated);
        eligible
    }

    /// The current best route, or `None` when there are no candidates. Advances
    /// the exploration counter. `exclude` hard-drops keys for this selection.
    pub fn pick_best(
        &mut self,
        now_ms: i64,
        exclude: &BTreeSet<String>,
    ) -> Option<RouteCandidate> {
        if self.candidates.is_empty() {
            return None;
        }
        self.pick_count += 1;
        self.ranked(now_ms, exclude).into_iter().next()
    }

    // ---- feedback hooks (caller-driven; the engine does no I/O) ----

    /// A reachability probe succeeded with round-trip `rtt_ms`.
    pub fn record_reachable(&mut self, key: &str, rtt_ms: i64, now_ms: i64) {
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.last_rtt_ms = Some(rtt_ms);
        stat.last_success = Some(now_ms);
        stat.consecutive_failures = 0;
        // A probe success clears a probe-armed quarantine, but NOT a task-path
        // (sticky) one — the health endpoint can be reachable while the task path
        // is broken. Only a real task success or expiry clears that.
        if !stat.sticky_quarantine {
            stat.quarantine_until = None;
        }
    }

    /// A reachability probe failed. Two in a row ⇒ quarantine until it recovers.
    pub fn record_unreachable(&mut self, key: &str, now_ms: i64) {
        let quarantine_ms = self.config.quarantine_ms;
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.last_rtt_ms = None;
        stat.consecutive_failures += 1;
        if stat.consecutive_failures >= 2 && !Self::quarantined(stat, now_ms) {
            stat.quarantine_until = Some(now_ms + quarantine_ms);
        }
    }

    /// Record a completed task's responsiveness (first-byte / first-audio ms),
    /// folded into the candidate's EWMA — the dominant ranking signal.
    pub fn record_task_latency(&mut self, key: &str, ms: i64, now_ms: i64) {
        let alpha = self.config.task_ewma_alpha;
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.task_ewma_ms = Some(match stat.task_ewma_ms {
            None => ms as f64,
            Some(prev) => prev * (1.0 - alpha) + ms as f64 * alpha,
        });
        stat.task_ewma_at = Some(now_ms);
    }

    /// A transport-level failure (socket refused/dropped, connected-but-no-output).
    /// Arms the cooldown so the route sinks without permanent exclusion.
    ///
    /// After a run of transport failures the cooldown alone isn't enough — the
    /// node keeps resurfacing and every request pays one failed attempt. At
    /// `transport_quarantine_threshold` it escalates to a sticky quarantine.
    pub fn record_transport_failure(&mut self, key: &str, now_ms: i64) {
        let cooldown_ms = self.config.cooldown_ms;
        let quarantine_ms = self.config.quarantine_ms;
        let threshold = self.config.transport_quarantine_threshold;
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.cooldown_until = Some(now_ms + cooldown_ms);
        stat.consecutive_transport_failures += 1;
        if stat.consecutive_transport_failures >= threshold {
            stat.quarantine_until = Some(now_ms + quarantine_ms);
            stat.sticky_quarantine = true;
        }
    }

    /// The service accepted the request but failed mid-use (stream dropped,
    /// final timed out, batch errored) — a stronger negative signal than a probe
    /// miss, so it quarantines immediately rather than waiting for a second
    /// strike.
    pub fn record_path_failure(&mut self, key: &str, now_ms: i64) {
        let quarantine_ms = self.config.quarantine_ms;
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.last_rtt_ms = None;
        stat.consecutive_failures = if stat.consecutive_failures < 2 {
            2
        } else {
            stat.consecutive_failures + 1
        };
        stat.quarantine_until = Some(now_ms + quarantine_ms);
        stat.sticky_quarantine = true;
    }

    /// A control-plane-issued capability/auth failure. The transport was never
    /// attempted, so reachability is unknown — this MUST NOT arm the cooldown (a
    /// transient control-plane blip would otherwise suppress an otherwise-healthy
    /// direct route).
    ///
    /// The no-op body is the correct behaviour, not a stub: this is the explicit
    /// classification point that keeps auth failures out of the reachability
    /// signal. Callers should route auth failures here rather than to
    /// [`Picker::record_transport_failure`].
    pub fn record_auth_failure(&mut self, _key: &str) {}

    /// Any successful use clears quarantine and cooldown.
    pub fn record_success(&mut self, key: &str, now_ms: i64) {
        let Some(stat) = self.stats.get_mut(key) else {
            return;
        };
        stat.last_success = Some(now_ms);
        stat.consecutive_failures = 0;
        stat.consecutive_transport_failures = 0;
        stat.quarantine_until = None;
        stat.cooldown_until = None;
        stat.sticky_quarantine = false;
    }

    /// Clear all quarantine/cooldown/failure state so every route is
    /// re-challenged from scratch. Intended for a network transition (e.g.
    /// cellular → mesh/wifi): a route quarantined because it was unreachable on
    /// the OLD network must not stay sunk for the full window after the NEW
    /// network makes it reachable. Latency EWMAs are kept — only the negative
    /// gates are dropped.
    pub fn reset_quarantines(&mut self) {
        for stat in self.stats.values_mut() {
            stat.quarantine_until = None;
            stat.cooldown_until = None;
            stat.consecutive_failures = 0;
            stat.consecutive_transport_failures = 0;
            stat.sticky_quarantine = false;
        }
    }

    // ---- introspection (telemetry + tests) ----

    pub fn rtt_ms(&self, key: &str) -> Option<i64> {
        self.stats.get(key).and_then(|s| s.last_rtt_ms)
    }

    pub fn task_ewma_ms(&self, key: &str) -> Option<f64> {
        self.stats.get(key).and_then(|s| s.task_ewma_ms)
    }

    pub fn is_quarantined(&mut self, key: &str, now_ms: i64) -> bool {
        self.stats
            .get_mut(key)
            .is_some_and(|s| Self::quarantined(s, now_ms))
    }

    pub fn is_cooling(&self, key: &str, now_ms: i64) -> bool {
        self.stats.get(key).is_some_and(|s| Self::cooling(s, now_ms))
    }

    /// The full read model: what would be chosen now, the ranked order, and the
    /// evidence behind each position. Observation only — does **not** advance the
    /// exploration counter, so polling it cannot perturb selection.
    pub fn snapshot(&mut self, now_ms: i64) -> RouteSnapshot {
        let order = self.ranked(now_ms, &BTreeSet::new());
        let refs: Vec<&RouteCandidate> = order.iter().collect();
        let scores = self.effective_scores(&refs, now_ms);
        let routes = order
            .iter()
            .map(|c| {
                let stat = self.stats.get(&c.key).cloned().unwrap_or_default();
                let mut probe = stat.clone();
                let state = if Self::quarantined(&mut probe, now_ms) {
                    RouteState::Quarantined
                } else if Self::cooling(&stat, now_ms) {
                    RouteState::Cooling
                } else {
                    RouteState::Eligible
                };
                let score = scores.get(&c.key).copied().filter(|v| v.is_finite());
                RouteObservation {
                    key: c.key.clone(),
                    kind: c.kind,
                    priority: c.priority,
                    probe_host: c.probe_host.clone(),
                    is_lan: c.is_lan(),
                    state,
                    score,
                    rtt_ms: stat.last_rtt_ms,
                    task_ewma_ms: stat.task_ewma_ms,
                    last_success_ms: stat.last_success,
                    quarantine_until_ms: stat.quarantine_until,
                    cooldown_until_ms: stat.cooldown_until,
                    consecutive_failures: stat.consecutive_failures,
                    consecutive_transport_failures: stat.consecutive_transport_failures,
                    sticky_quarantine: stat.sticky_quarantine,
                }
            })
            .collect::<Vec<_>>();
        RouteSnapshot {
            selected: routes.first().map(|r| r.key.clone()),
            routes,
            pick_count: self.pick_count,
        }
    }
}

// ---------------------------------------------------------------------------
// Route manifest
// ---------------------------------------------------------------------------

/// One route to one target, parsed from a control-plane route manifest.
///
/// The manifest schema is the other half of the capability: an engine with no
/// candidate feed just re-forks the feed instead. Shape:
///
/// ```json
/// { "<targets_key>": [ { "target_id": "asr", "label": "ASR",
///     "routes": [ { "route": "direct", "priority": 10, "relay_id": null,
///                   "health_url": "...", "ws_url": "...", "batch_url": "..." } ] } ] }
/// ```
///
/// Any string field whose name ends in `_url` is collected into [`RouteEntry::urls`],
/// so a producer can add transports without a schema change here.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteEntry {
    /// Stable picker key: `targetId:route[:relayId]` — **relay-aware**, so two
    /// relays to the same target keep distinct picker stats rather than
    /// colliding on one key.
    pub key: String,
    pub target_id: String,
    pub label: String,
    /// Raw advertised route string (`direct`, `regional_relay`, …).
    pub route: String,
    pub kind: RouteKind,
    pub priority: i32,
    pub relay_id: Option<String>,
    /// Per-route URL fields present in the manifest, keyed by field name.
    pub urls: BTreeMap<String, String>,
}

impl RouteEntry {
    pub fn url(&self, field: &str) -> Option<&str> {
        self.urls.get(field).map(|s| s.as_str())
    }
}

/// Parse the `targets_key` array of a route manifest into flat per-route
/// [`RouteEntry`]s, sorted by priority.
///
/// `default_target_id` is used when a target omits `target_id`. When the
/// resulting id is still empty the key falls back to a URL so it stays stable
/// and unique.
pub fn parse_route_manifest(
    manifest: &serde_json::Value,
    targets_key: &str,
    default_target_id: &str,
) -> Vec<RouteEntry> {
    let Some(targets) = manifest.get(targets_key).and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut out: Vec<RouteEntry> = Vec::new();
    for t in targets {
        let Some(target) = t.as_object() else { continue };
        let raw_target_id = target
            .get("target_id")
            .map(json_to_string)
            .unwrap_or_default();
        let raw_target_id = raw_target_id.trim().to_string();
        let target_id = if raw_target_id.is_empty() {
            default_target_id.to_string()
        } else {
            raw_target_id
        };
        let label = target
            .get("label")
            .filter(|v| !v.is_null())
            .map(json_to_string)
            .unwrap_or_else(|| {
                if target_id.is_empty() {
                    "target".to_string()
                } else {
                    target_id.clone()
                }
            });
        let Some(routes) = target.get("routes").and_then(|v| v.as_array()) else {
            continue;
        };
        for r in routes {
            let Some(route_obj) = r.as_object() else {
                continue;
            };
            let route = route_obj
                .get("route")
                .filter(|v| !v.is_null())
                .map(json_to_string)
                .unwrap_or_else(|| "unknown".to_string());
            let relay_id = route_obj
                .get("relay_id")
                .filter(|v| !v.is_null())
                .map(json_to_string);
            let priority = route_obj
                .get("priority")
                .and_then(|v| v.as_f64())
                .map(|v| v as i32)
                .unwrap_or(100);
            let mut urls = BTreeMap::new();
            for (field, value) in route_obj {
                if !field.ends_with("_url") || value.is_null() {
                    continue;
                }
                let v = json_to_string(value);
                if !v.is_empty() {
                    urls.insert(field.clone(), v);
                }
            }
            // Empty target id ⇒ fall back to a URL so the key is still unique.
            let base_id = if !target_id.is_empty() {
                target_id.clone()
            } else {
                urls.get("stream_url")
                    .or_else(|| urls.get("ws_url"))
                    .cloned()
                    .unwrap_or_else(|| route.clone())
            };
            let mut key = format!("{base_id}:{route}");
            if let Some(rid) = relay_id.as_ref().filter(|s| !s.is_empty()) {
                key.push(':');
                key.push_str(rid);
            }
            out.push(RouteEntry {
                key,
                target_id: target_id.clone(),
                label: label.clone(),
                kind: RouteKind::from_wire(&route),
                route,
                priority,
                relay_id,
                urls,
            });
        }
    }
    out.sort_by_key(|e| e.priority);
    out
}

fn json_to_string(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// Build picker candidates from parsed manifest entries. `probe_field` names the
/// URL field whose host the caller's reachability probe targets (e.g.
/// `health_url`); entries lacking it fall back to any URL they do carry.
pub fn candidates_from_entries(entries: &[RouteEntry], probe_field: &str) -> Vec<RouteCandidate> {
    entries
        .iter()
        .map(|e| {
            let probe = e
                .urls
                .get(probe_field)
                .or_else(|| e.urls.values().next())
                .cloned()
                .unwrap_or_default();
            RouteCandidate {
                key: e.key.clone(),
                kind: e.kind,
                priority: e.priority,
                probe_host: host_of(&probe),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Tests — ported from benchday `packages/mesh_route/test/mesh_route_test.dart`,
// which is the conformance corpus for this algorithm.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const T0: i64 = 1_700_000_000_000;

    fn none() -> BTreeSet<String> {
        BTreeSet::new()
    }

    fn excl(keys: &[&str]) -> BTreeSet<String> {
        keys.iter().map(|s| s.to_string()).collect()
    }

    fn picker_with(hosts: &[(&str, &str)]) -> Picker {
        let mut p = Picker::default();
        p.set_candidates(
            hosts
                .iter()
                .map(|(k, h)| RouteCandidate::new(*k, RouteKind::Direct, *h))
                .collect(),
        );
        p
    }

    #[test]
    fn is_lan_classifies_rfc1918_but_not_cgnat_mesh() {
        assert!(is_lan_host("192.168.1.5"));
        assert!(is_lan_host("10.0.0.1"));
        assert!(is_lan_host("172.16.0.1"));
        assert!(is_lan_host("172.31.255.255"));
        assert!(!is_lan_host("172.32.0.1"));
        assert!(!is_lan_host("100.64.0.3"));
        assert!(!is_lan_host("8.8.8.8"));
    }

    #[test]
    fn is_lan_host_accepts_bare_hosts_and_urls() {
        assert!(is_lan_host("ws://192.168.1.5:7778/"));
        assert!(is_lan_host("http://10.1.2.3:8080/health"));
        assert!(!is_lan_host("ws://100.64.0.3:7778/"));
        assert!(!is_lan_host("not a url"));
    }

    #[test]
    fn mesh_and_private_host_classification() {
        assert!(is_mesh_host("100.64.0.3"));
        assert!(is_mesh_host("100.127.255.1"));
        assert!(!is_mesh_host("100.128.0.1"));
        assert!(!is_mesh_host("192.168.1.5"));
        assert!(is_private_host("192.168.1.5"));
        assert!(is_private_host("100.64.0.3"));
        assert!(!is_private_host("8.8.8.8"));
    }

    #[test]
    fn classify_direct_host_buckets_by_class() {
        assert_eq!(classify_direct_host("192.168.1.5"), AddressClass::Private);
        assert_eq!(classify_direct_host("100.64.0.3"), AddressClass::Mesh);
        assert_eq!(classify_direct_host("example.com"), AddressClass::Hostname);
        assert_eq!(classify_direct_host("8.8.8.8"), AddressClass::PublicIp);
        assert_eq!(
            classify_direct_host("localhost"),
            AddressClass::LoopbackOrWildcard
        );
        assert_eq!(
            classify_direct_host("0.0.0.0"),
            AddressClass::LoopbackOrWildcard
        );
    }

    #[test]
    fn parse_direct_endpoint_accepts_and_canonicalizes() {
        let policy = EndpointPolicy::default();
        let ep = parse_direct_endpoint(Some("ws://192.168.1.5:7778/x"), &policy).unwrap();
        assert_eq!(ep.uri, "ws://192.168.1.5:7778/");
        assert_eq!(ep.target(), "192.168.1.5:7778");
        assert_eq!(ep.address_class, AddressClass::Private);
        assert!(ep.is_private());

        let mesh = parse_direct_endpoint(Some("ws://100.64.0.3:7778/"), &policy).unwrap();
        assert!(mesh.is_mesh());

        // Scheme-defaulted port, omitted again on canonicalization.
        let named = parse_direct_endpoint(Some("wss://daemon.example.com/"), &policy).unwrap();
        assert_eq!(named.port, 443);
        assert_eq!(named.uri, "wss://daemon.example.com/");
        assert_eq!(named.address_class, AddressClass::Hostname);
    }

    #[test]
    fn parse_direct_endpoint_rejects_bad_input() {
        let policy = EndpointPolicy::default();
        assert!(parse_direct_endpoint(None, &policy).is_err());
        assert!(parse_direct_endpoint(Some("   "), &policy).is_err());
        assert!(parse_direct_endpoint(Some("http://192.168.1.5:80/"), &policy).is_err());
        assert!(parse_direct_endpoint(Some("ws://8.8.8.8:80/"), &policy).is_err());
        assert!(parse_direct_endpoint(Some("ws://127.0.0.1:7778/"), &policy).is_err());
        assert!(parse_direct_endpoint(Some("ws://192.168.1.5:99999/"), &policy).is_err());
    }

    #[test]
    fn endpoint_policy_makes_the_gate_transport_agnostic() {
        // The reference implementation hardcoded ws/wss; a policy turns the same
        // validator into an HTTP (or anything) gate without a fork.
        let http = EndpointPolicy::http();
        let ep = parse_direct_endpoint(Some("https://100.64.0.3/health"), &http).unwrap();
        assert_eq!(ep.port, 443);
        assert!(parse_direct_endpoint(Some("ws://100.64.0.3:7778/"), &http).is_err());

        let anything = EndpointPolicy::any_scheme();
        let grpc = parse_direct_endpoint(Some("grpc://100.64.0.3:50051"), &anything).unwrap();
        assert_eq!(grpc.port, 50051);
        // An unknown scheme has no default port, so one must be written.
        assert!(parse_direct_endpoint(Some("grpc://100.64.0.3"), &anything).is_err());
    }

    #[test]
    fn loopback_allowed_only_when_policy_says_so() {
        let policy = EndpointPolicy {
            allow_loopback: true,
            ..EndpointPolicy::default()
        };
        let ep = parse_direct_endpoint(Some("ws://127.0.0.1:7778/"), &policy).unwrap();
        assert_eq!(ep.address_class, AddressClass::LoopbackOrWildcard);
    }

    #[test]
    fn route_kind_maps_the_wire_strings() {
        assert_eq!(RouteKind::from_wire("direct"), RouteKind::Direct);
        assert_eq!(
            RouteKind::from_wire("regional_relay"),
            RouteKind::RegionalRelay
        );
        assert_eq!(RouteKind::from_wire("hub_proxy"), RouteKind::HubProxy);
        assert_eq!(RouteKind::from_wire("webrtc_p2p"), RouteKind::WebRtcP2p);
        assert_eq!(RouteKind::from_wire("nonsense"), RouteKind::Direct);
    }

    #[test]
    fn ranks_lower_rtt_first() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 90, T0);
        p.record_reachable("b", 20, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "b");
    }

    #[test]
    fn learned_task_latency_overrides_raw_rtt() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 90, T0);
        p.record_reachable("b", 20, T0);
        // b pings faster but is far slower to actually produce output.
        p.record_task_latency("a", 300, T0);
        p.record_task_latency("b", 3000, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "a");
    }

    #[test]
    fn two_unreachable_probes_quarantine_and_success_clears() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);

        p.record_unreachable("a", T0);
        assert!(!p.is_quarantined("a", T0));
        p.record_unreachable("a", T0);
        assert!(p.is_quarantined("a", T0));
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "b");

        // Quarantine expiry restores eligibility.
        let later = T0 + 61_000;
        assert!(!p.is_quarantined("a", later));

        p.record_reachable("a", 10, later);
        p.record_success("a", later);
        assert!(!p.is_quarantined("a", later));
    }

    #[test]
    fn reset_quarantines_rechallenges_every_route() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_task_latency("a", 400, T0);
        p.record_unreachable("a", T0);
        p.record_unreachable("a", T0);
        p.record_transport_failure("b", T0);
        assert!(p.is_quarantined("a", T0));
        assert!(p.is_cooling("b", T0));

        p.reset_quarantines();

        assert!(!p.is_quarantined("a", T0));
        assert!(!p.is_cooling("b", T0));
        // Latency learning survives a network transition; only gates are dropped.
        assert_eq!(p.task_ewma_ms("a"), Some(400.0));
    }

    #[test]
    fn transport_failure_arms_cooldown_but_auth_failure_does_not() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);

        p.record_transport_failure("a", T0);
        assert!(p.is_cooling("a", T0));
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "b");
        assert!(!p.is_cooling("a", T0 + 11_000));

        // An auth failure never happened on the wire — it must not suppress an
        // otherwise-healthy route.
        p.record_auth_failure("a");
        assert!(!p.is_cooling("a", T0 + 11_000));
        assert!(!p.is_quarantined("a", T0 + 11_000));
    }

    #[test]
    fn exploration_periodically_surfaces_the_runner_up() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 12, T0);

        let mut picks = Vec::new();
        for _ in 0..10 {
            picks.push(p.pick_best(T0, &none()).unwrap().key);
        }
        // The favorite dominates but the runner-up is re-measured periodically.
        assert!(picks.iter().filter(|k| *k == "b").count() >= 2);
        assert!(picks.iter().any(|k| k == "a"));
    }

    #[test]
    fn exploration_is_gated_by_the_latency_band() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 5000, T0);
        p.record_task_latency("a", 100, T0);
        p.record_task_latency("b", 9000, T0);

        for _ in 0..12 {
            // b is far outside the 1.5x band, so it is never explored.
            assert_eq!(p.pick_best(T0, &none()).unwrap().key, "a");
        }
    }

    #[test]
    fn lan_then_kind_then_priority_break_score_ties() {
        let mut p = Picker::default();
        p.set_candidates(vec![
            RouteCandidate::new("mesh", RouteKind::Direct, "100.64.0.3").with_priority(1),
            RouteCandidate::new("lan", RouteKind::Direct, "192.168.1.5").with_priority(9),
        ]);
        p.record_reachable("mesh", 20, T0);
        p.record_reachable("lan", 20, T0);
        // Equal score ⇒ LAN wins despite the worse advertised priority.
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "lan");

        let mut q = Picker::default();
        q.set_candidates(vec![
            RouteCandidate::new("relay", RouteKind::RegionalRelay, "100.64.0.3").with_priority(1),
            RouteCandidate::new("direct", RouteKind::Direct, "100.64.0.4").with_priority(9),
        ]);
        q.record_reachable("relay", 20, T0);
        q.record_reachable("direct", 20, T0);
        // Equal score, neither is LAN ⇒ the direct kind wins over the relay.
        assert_eq!(q.pick_best(T0, &none()).unwrap().key, "direct");

        let mut r = Picker::default();
        r.set_candidates(vec![
            RouteCandidate::new("lo", RouteKind::Direct, "100.64.0.3").with_priority(9),
            RouteCandidate::new("hi", RouteKind::Direct, "100.64.0.4").with_priority(1),
        ]);
        r.record_reachable("lo", 20, T0);
        r.record_reachable("hi", 20, T0);
        // Everything else equal ⇒ advertised priority decides.
        assert_eq!(r.pick_best(T0, &none()).unwrap().key, "hi");
    }

    #[test]
    fn set_candidates_preserves_stats_and_drops_departed_keys() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 42, T0);
        p.record_task_latency("a", 250, T0);

        p.set_candidates(vec![
            RouteCandidate::new("a", RouteKind::Direct, "100.64.0.2"),
            RouteCandidate::new("c", RouteKind::Direct, "100.64.0.4"),
        ]);

        assert_eq!(p.rtt_ms("a"), Some(42));
        assert_eq!(p.task_ewma_ms("a"), Some(250.0));
        assert_eq!(p.rtt_ms("b"), None);
        assert_eq!(p.rtt_ms("c"), None);
    }

    #[test]
    fn exclude_hard_drops_a_key_for_one_shot_failover() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "a");
        // The caller just failed on `a` and hasn't reported it yet.
        assert_eq!(p.pick_best(T0, &excl(&["a"])).unwrap().key, "b");
        // Exclusion is one-shot, not sticky state.
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "a");
    }

    #[test]
    fn record_path_failure_quarantines_on_one_strike() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);
        p.record_path_failure("a", T0);
        assert!(p.is_quarantined("a", T0));
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "b");
    }

    #[test]
    fn task_path_quarantine_survives_a_probe_success() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);
        p.record_path_failure("a", T0);

        // The dictation-stall shape: /health answers 200 while the task path is
        // broken. A probe success must NOT resurrect the route.
        p.record_reachable("a", 10, T0 + 1_000);
        assert!(p.is_quarantined("a", T0 + 1_000));
        assert_eq!(p.pick_best(T0 + 1_000, &none()).unwrap().key, "b");

        // A real task success does clear it.
        p.record_success("a", T0 + 2_000);
        assert!(!p.is_quarantined("a", T0 + 2_000));
    }

    #[test]
    fn task_path_quarantine_also_clears_on_expiry() {
        let mut p = picker_with(&[("a", "100.64.0.2")]);
        p.record_reachable("a", 10, T0);
        p.record_path_failure("a", T0);
        assert!(p.is_quarantined("a", T0));
        assert!(!p.is_quarantined("a", T0 + 61_000));
    }

    #[test]
    fn probe_armed_quarantine_clears_on_a_probe_success() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_unreachable("a", T0);
        p.record_unreachable("a", T0);
        assert!(p.is_quarantined("a", T0));
        // No task-path failure was involved, so a probe success is sufficient.
        p.record_reachable("a", 10, T0 + 1_000);
        assert!(!p.is_quarantined("a", T0 + 1_000));
    }

    #[test]
    fn transport_failures_escalate_to_quarantine_after_three() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);

        p.record_transport_failure("a", T0);
        assert!(p.is_cooling("a", T0));
        assert!(!p.is_quarantined("a", T0));
        // Cooldown lapses and the broken node resurfaces...
        p.record_transport_failure("a", T0 + 11_000);
        assert!(!p.is_quarantined("a", T0 + 11_000));
        // ...until the third strike escalates it to a sticky quarantine.
        p.record_transport_failure("a", T0 + 22_000);
        assert!(p.is_quarantined("a", T0 + 22_000));

        // Sticky: a healthy /health probe does not resurrect it.
        p.record_reachable("a", 10, T0 + 23_000);
        assert!(p.is_quarantined("a", T0 + 23_000));

        // A real success resets the escalation counter too.
        p.record_success("a", T0 + 24_000);
        assert!(!p.is_quarantined("a", T0 + 24_000));
        p.record_transport_failure("a", T0 + 25_000);
        assert!(!p.is_quarantined("a", T0 + 25_000));
    }

    #[test]
    fn an_unmeasured_node_does_not_dethrone_a_proven_fast_one() {
        let mut p = picker_with(&[("proven", "100.64.0.2"), ("fresh", "100.64.0.3")]);
        p.record_reachable("proven", 200, T0);
        p.record_task_latency("proven", 400, T0);
        // A freshly advertised node pings fast but has served nothing.
        p.record_reachable("fresh", 20, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "proven");
    }

    #[test]
    fn an_unmeasured_node_bootstraps_ahead_of_a_proven_slow_one() {
        let mut p = picker_with(&[
            ("fast", "100.64.0.2"),
            ("slow", "100.64.0.3"),
            ("fresh", "100.64.0.4"),
        ]);
        p.record_reachable("fast", 30, T0);
        p.record_task_latency("fast", 200, T0); // best measured ≈ .6*200+.4*30 = 132
        p.record_reachable("slow", 30, T0);
        p.record_task_latency("slow", 5000, T0); // ≈ 3012
        p.record_reachable("fresh", 20, T0); // unmeasured → bootstraps to 132

        let order: Vec<String> = p
            .ranked(T0, &none())
            .into_iter()
            .map(|c| c.key)
            .collect();
        // fast first; fresh bootstraps to fast's score but sits behind it at the
        // tie (a real measurement beats a bootstrap). The load-bearing point is
        // that fresh outranks the proven-slow node — pessimism isn't burial.
        assert_eq!(order[0], "fast");
        let idx = |k: &str| order.iter().position(|x| x == k).unwrap();
        assert!(idx("fresh") < idx("slow"));
    }

    #[test]
    fn cold_start_with_no_ewmas_ranks_by_plain_rtt() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 80, T0);
        p.record_reachable("b", 25, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "b");
    }

    #[test]
    fn a_stale_ewma_is_forgiven_against_a_freshly_measured_rival() {
        let mut p = Picker::new(PickerConfig {
            explore_every: 0,
            task_ewma_half_life_ms: 10 * 60_000,
            ..PickerConfig::default()
        });
        p.set_candidates(vec![
            RouteCandidate::new("a", RouteKind::Direct, "100.64.0.2"),
            RouteCandidate::new("b", RouteKind::Direct, "100.64.0.3"),
        ]);
        p.record_reachable("a", 20, T0);
        p.record_task_latency("a", 4000, T0); // a's one-off bad boot sample

        // Two hours later a still pings fast; its ancient 4000 EWMA should no
        // longer dominate. b shows up freshly measured and merely OK.
        let later = T0 + 2 * 3_600_000;
        p.record_reachable("a", 20, later);
        p.record_reachable("b", 25, later);
        p.record_task_latency("b", 300, later); // ≈ .6*300+.4*25 = 190
        // a's decayed score ≈ its rtt (20) ≪ 190, so the bad boot is forgiven.
        assert_eq!(p.pick_best(later, &none()).unwrap().key, "a");
    }

    #[test]
    fn a_stale_ewma_decays_toward_fresh_rtt() {
        let mut p = picker_with(&[("stale", "100.64.0.2")]);
        p.record_reachable("stale", 20, T0);
        p.record_task_latency("stale", 5000, T0);
        let fresh_score = p.snapshot(T0).routes[0].score.unwrap();
        // Many half-lives later the one bad sample no longer dominates.
        let aged_score = p.snapshot(T0 + 10 * 15 * 60_000).routes[0].score.unwrap();
        assert!(aged_score < fresh_score);
        assert!(aged_score < 100.0);
    }

    #[test]
    fn gated_routes_still_rank_as_a_least_bad_tail() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "100.64.0.3")]);
        p.record_reachable("a", 10, T0);
        p.record_reachable("b", 50, T0);
        p.record_path_failure("a", T0);
        p.record_path_failure("b", T0);
        // Everything is quarantined, but a pick is still possible.
        let picked = p.pick_best(T0, &none());
        assert!(picked.is_some());
        assert_eq!(p.ranked(T0, &none()).len(), 2);
    }

    #[test]
    fn no_candidates_means_no_pick() {
        let mut p = Picker::default();
        assert!(p.pick_best(T0, &none()).is_none());
        assert_eq!(p.pick_count(), 0);
    }

    #[test]
    fn snapshot_reports_state_and_does_not_perturb_selection() {
        let mut p = picker_with(&[("a", "100.64.0.2"), ("b", "192.168.1.5")]);
        p.record_reachable("a", 90, T0);
        p.record_reachable("b", 20, T0);
        p.record_transport_failure("a", T0);

        let before = p.pick_count();
        let snap = p.snapshot(T0);
        assert_eq!(p.pick_count(), before, "snapshot must not advance the counter");

        assert_eq!(snap.selected.as_deref(), Some("b"));
        assert_eq!(snap.routes.len(), 2);
        let b = snap.routes.iter().find(|r| r.key == "b").unwrap();
        assert_eq!(b.state, RouteState::Eligible);
        assert_eq!(b.rtt_ms, Some(20));
        assert!(b.is_lan);
        let a = snap.routes.iter().find(|r| r.key == "a").unwrap();
        assert_eq!(a.state, RouteState::Cooling);
        assert_eq!(a.cooldown_until_ms, Some(T0 + 10_000));
    }

    #[test]
    fn snapshot_serializes_for_the_wire() {
        let mut p = picker_with(&[("a", "100.64.0.2")]);
        p.record_reachable("a", 30, T0);
        let json = serde_json::to_string(&p.snapshot(T0)).unwrap();
        assert!(json.contains("\"selected\":\"a\""));
        assert!(json.contains("\"kind\":\"direct\""));
        assert!(json.contains("\"state\":\"eligible\""));
    }

    #[test]
    fn manifest_builds_relay_aware_keys_sorted_by_priority() {
        let manifest = serde_json::json!({
            "asr_targets": [{
                "target_id": "asr",
                "label": "ASR",
                "routes": [
                    {"route": "regional_relay", "relay_id": "cn-1", "priority": 20,
                     "health_url": "https://relay1/health", "ws_url": "wss://relay1/ws"},
                    {"route": "regional_relay", "relay_id": "cn-2", "priority": 30,
                     "health_url": "https://relay2/health", "ws_url": "wss://relay2/ws"},
                    {"route": "direct", "priority": 10,
                     "health_url": "http://100.64.0.3:8766/health",
                     "ws_url": "ws://100.64.0.3:8766/ws"}
                ]
            }]
        });
        let entries = parse_route_manifest(&manifest, "asr_targets", "asr");
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0].key, "asr:direct");
        assert_eq!(entries[0].kind, RouteKind::Direct);
        // Two relays to the same target keep DISTINCT keys — the collision the
        // relay-aware scheme exists to prevent.
        assert_eq!(entries[1].key, "asr:regional_relay:cn-1");
        assert_eq!(entries[2].key, "asr:regional_relay:cn-2");
        assert_eq!(entries[1].kind, RouteKind::RegionalRelay);
        assert_eq!(
            entries[0].url("ws_url"),
            Some("ws://100.64.0.3:8766/ws")
        );
    }

    #[test]
    fn manifest_falls_back_to_a_url_when_target_id_is_empty() {
        let manifest = serde_json::json!({
            "tts_targets": [{
                "routes": [
                    {"route": "direct", "stream_url": "ws://100.64.0.2:8770/stream"}
                ]
            }]
        });
        let entries = parse_route_manifest(&manifest, "tts_targets", "");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].key, "ws://100.64.0.2:8770/stream:direct");
        assert_eq!(entries[0].priority, 100);
    }

    #[test]
    fn manifest_collects_any_url_field_so_new_transports_need_no_schema_change() {
        let manifest = serde_json::json!({
            "targets": [{
                "target_id": "t",
                "routes": [{"route": "direct", "grpc_url": "grpc://100.64.0.3:50051",
                            "quic_url": "quic://100.64.0.3:4433"}]
            }]
        });
        let entries = parse_route_manifest(&manifest, "targets", "");
        assert_eq!(entries[0].url("grpc_url"), Some("grpc://100.64.0.3:50051"));
        assert_eq!(entries[0].url("quic_url"), Some("quic://100.64.0.3:4433"));
    }

    #[test]
    fn manifest_tolerates_junk() {
        assert!(parse_route_manifest(&serde_json::json!({}), "x", "").is_empty());
        assert!(parse_route_manifest(&serde_json::json!({"x": 3}), "x", "").is_empty());
        let junk = serde_json::json!({"x": [3, {"routes": "nope"}, {"routes": [7]}]});
        assert!(parse_route_manifest(&junk, "x", "").is_empty());
    }

    #[test]
    fn candidates_from_entries_probes_the_named_field() {
        let manifest = serde_json::json!({
            "asr_targets": [{
                "target_id": "asr",
                "routes": [{"route": "direct", "priority": 10,
                            "health_url": "http://192.168.1.5:8766/health",
                            "ws_url": "ws://192.168.1.5:8766/ws"}]
            }]
        });
        let entries = parse_route_manifest(&manifest, "asr_targets", "asr");
        let cands = candidates_from_entries(&entries, "health_url");
        assert_eq!(cands.len(), 1);
        assert_eq!(cands[0].probe_host, "192.168.1.5");
        assert!(cands[0].is_lan());
        assert_eq!(cands[0].priority, 10);
    }

    #[test]
    fn end_to_end_manifest_to_selection_to_snapshot() {
        let manifest = serde_json::json!({
            "asr_targets": [{
                "target_id": "asr",
                "label": "ASR",
                "routes": [
                    {"route": "direct", "priority": 10,
                     "health_url": "http://100.64.0.3:8766/health"},
                    {"route": "regional_relay", "relay_id": "cn-1", "priority": 20,
                     "health_url": "https://relay1/health"}
                ]
            }]
        });
        let entries = parse_route_manifest(&manifest, "asr_targets", "asr");
        let mut p = Picker::default();
        p.set_candidates(candidates_from_entries(&entries, "health_url"));

        p.record_reachable("asr:direct", 25, T0);
        p.record_reachable("asr:regional_relay:cn-1", 180, T0);
        assert_eq!(p.pick_best(T0, &none()).unwrap().key, "asr:direct");

        // The direct path dies mid-stream; the relay takes over and the snapshot
        // explains why — this is what a live topology view renders.
        p.record_path_failure("asr:direct", T0 + 5_000);
        let snap = p.snapshot(T0 + 5_000);
        assert_eq!(snap.selected.as_deref(), Some("asr:regional_relay:cn-1"));
        let direct = snap.routes.iter().find(|r| r.key == "asr:direct").unwrap();
        assert_eq!(direct.state, RouteState::Quarantined);
        assert!(direct.sticky_quarantine);
    }
}
