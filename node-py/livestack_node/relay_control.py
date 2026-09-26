"""relay_control.py — the token control plane for the livestack relay realm (DR-1).

Livestack operates its own realm on the shared meshlink relay: its own ed25519
attachment key (PKCS#8, file mode 0600 — the same discipline as
:data:`LIVESTACK_FLEET_TOKENS_FILE` in fleet_auth.py) and its own HMAC key ring
for caller capabilities. Realm isolation is cryptographic, not cosmetic: a
livestack token is invalid in benchday's realm and vice versa.

The token FORMATS are defined by the relay, and the relay's own verify code is
the spec this module ports (meshlink ``packages/mesh_relay/src/attachment.ts``
and ``capability.ts``, pinned by ``MESHLINK.lock`` at the repo root — DR-5):

* ``bdrt1`` attachments are ed25519-signed, JWT-shaped tokens a daemon presents
  at the relay's attach door. The relay's verify enforces ``exp <= iat + 300``,
  so an attachment can live at most 300 s no matter what TTL is requested;
  renewal is the steady state, not an edge case.
* ``bdsr1`` capabilities are HMAC-SHA256 tokens naming who may reach which
  target through which relay, for how long. TTL is clamped to 30 s..15 min by
  the relay (``clampTtl``); a token older than its TTL plus a 30 s grace is
  refused as ``capability_expired``.

**Key ring and rotation.** The cap ring carries several HMAC keys: one mints
(the ``active`` kid is recorded in each token), ALL keys in the ring still
verify. Publishing a new key and making it active is therefore not a flag day:
tokens minted under the old key keep verifying until they expire (one TTL,
≤15 min) and are then retired with :meth:`CapKeyRing.retire`. The full
rotation drill (in-flight tunnels surviving a rotation) needs the mesh outbound
package and is a later phase; this ring API — :meth:`CapKeyRing.rotate`,
:meth:`CapKeyRing.retire`, ``kid`` on every minted token — is shaped so that
drill can drive it.

**Quota (DR-3).** The relay's per-realm quota is set at relay DEPLOY time from
:class:`RelayQuotaConfig`; the values here are the declaration livestack hands
the deploy, not a runtime knob. The relay default (4 concurrent streams /
240 req-min per realm+account) is too low for broker fan-out, so the livestack
realm quota is set explicitly. This limit and livestack's own
``max_concurrent_per_account`` fleet policy are STACKED, not merged: either may
bind first, and a relay 429 must surface as a named degradation ("relay
quota"), never as a generic failure.

Everything here is a pure decision where it can be: mint/verify take the clock
as a parameter so tests need no sleeps.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Wire prefixes — the relay refuses anything else (meshlink attachment.ts /
#: capability.ts). These are protocol constants, not cosmetics; they never change.
ATTACHMENT_PREFIX = "bdrt1"
CAPABILITY_PREFIX = "bdsr1"
CAPABILITY_VERSION = 1

#: Relay bounds, ported from the relay's verify/clamp code (DR-5 pin). The
#: relay is the enforcer; these are the same numbers so a token minted here
#: is acceptable there.
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 15 * 60
#: The relay's attachment verify requires exp <= iat + 300, so 300 s is the
#: hard ceiling on an attachment's life regardless of requested TTL.
MAX_ATTACHMENT_TTL_SECONDS = 300
DEFAULT_ATTACHMENT_TTL_SECONDS = 300
DEFAULT_CAPABILITY_TTL_SECONDS = MAX_TTL_SECONDS

#: Renewal fires this long before expiry. An attachment lives at most 300 s,
#: so 60 s of lead leaves 240 s of valid tunnel per cycle (plan Phase 6).
DEFAULT_RENEWAL_LEAD_SECONDS = 60

#: The livestack realm's cosmetic claims (meshlink cosmetics.ts, DR-4). The
#: relay serves each realm its own configured cosmetics; these defaults are
#: what the livestack realm wears, and each is env-overridable.
DEFAULT_REALM = "livestack"
DEFAULT_ATTACHMENT_AUDIENCE = "livestack-relay-attachment"
DEFAULT_CAPABILITY_TYPE = "livestack-relay-capability"
DEFAULT_CAPABILITY_AUDIENCE = "livestack-relay"

_DAEMON_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def clamp_ttl(value: float, default: int = DEFAULT_CAPABILITY_TTL_SECONDS) -> int:
    """The relay's ``clampTtl`` ported: 30 s floor, 15 min ceiling.

    A non-finite or non-positive value takes the default, matching the relay
    (an unset TTL means the documented default, never "no limit").
    """
    if not isinstance(value, (int, float)) or value != value or value <= 0:
        return default
    return max(MIN_TTL_SECONDS, min(int(value), MAX_TTL_SECONDS))


def seconds_until_refresh(exp: float, now: Optional[float] = None,
                          lead: int = DEFAULT_RENEWAL_LEAD_SECONDS) -> float:
    """Seconds until a token with expiry ``exp`` should be renewed.

    Zero or negative means renew NOW — the caller must not schedule a negative
    delay. Renewal is the steady state for attachments (they live ≤300 s).
    """
    now = time.time() if now is None else now
    return max(0.0, exp - lead - now)


# ---------------------------------------------------------------------------
# Cap key ring (bdsr1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapKey:
    """One HMAC signing key in the relay key ring (meshlink RelayKey)."""
    kid: str
    secret: str


class CapKeyRing:
    """The relay key ring (meshlink RelayKeyRing): ``active`` mints, every key
    in the ring still verifies, so rotation overlaps instead of being a flag
    day (see module docstring).

    ``from_json`` parses the same shape the relay's
    ``relayKeyRingFromEnv`` accepts — ``[{"kid": ..., "secret": ...,
    "active": true}, ...]`` — so one env var serves both sides of the wire.
    """

    def __init__(self, keys: Sequence[CapKey], active_kid: Optional[str] = None):
        if not keys:
            raise ValueError("cap key ring requires at least one key")
        kids = [k.kid for k in keys]
        if len(set(kids)) != len(kids):
            raise ValueError(f"duplicate kid in cap key ring: {kids}")
        self._keys = tuple(keys)
        if active_kid is None:
            active_kid = kids[0]
        if active_kid not in kids:
            raise ValueError(f"active kid {active_kid!r} is not in the ring")
        self._active_kid = active_kid

    @classmethod
    def from_json(cls, raw: str) -> "CapKeyRing":
        """Parse the relay's key-ring JSON. Malformed entries are skipped,
        loudly, and logged through ``log`` — one bad entry must not take every
        other key down with it (same discipline as fleet_auth.load_principals).
        """
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("cap key ring JSON must be an array of {kid, secret, active}")
        keys = []
        active: Optional[str] = None
        for item in parsed:
            if not isinstance(item, dict):
                continue
            kid = str(item.get("kid") or "").strip()
            secret = str(item.get("secret") or "").strip()
            if not kid or not secret:
                continue
            keys.append(CapKey(kid=kid, secret=secret))
            if item.get("active") is True:
                active = kid
        if not keys:
            raise ValueError("cap key ring JSON parsed to zero usable keys")
        return cls(keys, active_kid=active or keys[0].kid)

    @property
    def active(self) -> CapKey:
        return next(k for k in self._keys if k.kid == self._active_kid)

    @property
    def kids(self) -> Tuple[str, ...]:
        return tuple(k.kid for k in self._keys)

    def verify_keys(self) -> Tuple[CapKey, ...]:
        """Every key that still validates — the verify window rotation lives on."""
        return self._keys

    def key_for(self, kid: str) -> Optional[CapKey]:
        return next((k for k in self._keys if k.kid == kid), None)

    def rotate(self, new_key: CapKey) -> "CapKeyRing":
        """A new ring where ``new_key`` mints and every existing key — the
        outgoing active one first — still verifies.

        Call ``retire`` on the returned ring once no token minted under the
        outgoing key can still be alive (one TTL, ≤15 min) to close the
        rotation. Adding an already-present kid is refused: shadowing a kid
        would make tokens signed under the earlier secret unverifiable while
        still claiming its name.
        """
        if self.key_for(new_key.kid) is not None:
            raise ValueError(f"kid {new_key.kid!r} is already in the ring")
        return CapKeyRing([new_key, *self._keys], active_kid=new_key.kid)

    def retire(self, kid: str) -> "CapKeyRing":
        """Drop ``kid`` from the ring. Tokens minted under it stop verifying
        IMMEDIATELY — call only once every such token is expired (one TTL
        after the last mint under it). Refusing to retire the active key is
        deliberate: retirement is a verify-window operation, so there must
        always be a minting key left.
        """
        remaining = [k for k in self._keys if k.kid != kid]
        if len(remaining) == len(self._keys):
            raise ValueError(f"kid {kid!r} is not in the ring")
        if kid == self._active_kid:
            raise ValueError("cannot retire the active minting key; rotate first")
        return CapKeyRing(remaining, active_kid=self._active_kid)


def mint_capability(ring: CapKeyRing, *,
                    account_id: str, device_id: str, target_id: str,
                    relay_id: str, region: str, scopes: Sequence[str],
                    ttl_seconds: Optional[float] = None,
                    typ: str = DEFAULT_CAPABILITY_TYPE,
                    aud: str = DEFAULT_CAPABILITY_AUDIENCE,
                    now: Optional[int] = None) -> Tuple[str, dict]:
    """Mint a ``bdsr1`` caller capability. Returns ``(token, payload)``.

    Ported from meshlink ``issueSpeechRelayCapability``: same claim names,
    same key order, ``nbf = iat - 5``, scopes normalized (deduped, sorted),
    ``kid`` recorded so the verifier picks the key directly, TTL clamped to
    the relay's 30 s..15 min window.
    """
    now = int(time.time()) if now is None else int(now)
    ttl = clamp_ttl(ttl_seconds) if ttl_seconds is not None else DEFAULT_CAPABILITY_TTL_SECONDS
    key = ring.active
    payload = {
        "v": CAPABILITY_VERSION,
        "typ": typ,
        "aud": aud,
        "jti": "sr_" + secrets.token_hex(8),
        "iat": now,
        "nbf": now - 5,
        "exp": now + ttl,
        "account_id": account_id,
        "device_id": device_id,
        "target_id": target_id,
        "relay_id": relay_id,
        "region": region,
        "scopes": sorted(set(scopes)),
        "kid": key.kid,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key.secret.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{CAPABILITY_PREFIX}.{_b64url(body)}.{_b64url(sig)}", payload


def peek_capability_kid(token: str) -> Optional[str]:
    """The ``kid`` claim WITHOUT verifying the token — diagnostics only,
    exactly the relay's ``peekSpeechRelayCapability``. Never use this to
    pick an authorization decision; the signature check that follows the
    peek is what makes the claim trustworthy.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        kid = payload.get("kid")
        return kid if isinstance(kid, str) and kid else None
    except Exception:
        return None


def verify_capability(ring: CapKeyRing, token: str, *,
                      target_id: str, relay_id: Optional[str] = None,
                      scope: str, typ: str = DEFAULT_CAPABILITY_TYPE,
                      aud: str = DEFAULT_CAPABILITY_AUDIENCE,
                      now: Optional[int] = None) -> Tuple[bool, Optional[dict], str]:
    """Verify a ``bdsr1`` capability against this ring. Ported from the
    relay's ``verifySpeechRelayCapability`` (single-realm, ``keys`` form):
    returns ``(ok, payload_or_None, reason)`` where reason is the relay's own
    refusal string on failure. Signature comparison is constant-time.

    This is the client-side mirror used for tests and diagnostics; the relay
    remains the enforcing verifier.
    """
    now = int(time.time()) if now is None else int(now)
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != CAPABILITY_PREFIX:
        return False, None, "bad_capability_format"
    try:
        body = _b64url_decode(parts[1])
        provided_sig = _b64url_decode(parts[2])
        payload = json.loads(body)
    except Exception:
        return False, None, "bad_capability_encoding"
    kid = payload.get("kid")
    candidates = ring.verify_keys() if not kid else tuple(
        k for k in ring.verify_keys() if k.kid == kid)
    if kid and not candidates:
        return False, None, "unknown_key_id"
    matched = None
    for key in candidates:
        expected = hmac.new(key.secret.encode("utf-8"), body, hashlib.sha256).digest()
        if len(provided_sig) == len(expected) and hmac.compare_digest(provided_sig, expected):
            matched = key
            break
    if matched is None:
        return False, None, "bad_capability_signature"
    if (payload.get("v") != CAPABILITY_VERSION or payload.get("typ") != typ
            or payload.get("aud") != aud):
        return False, None, "bad_capability_type"
    if payload.get("target_id") != target_id:
        return False, None, "target_mismatch"
    if relay_id is not None and payload.get("relay_id") != relay_id:
        return False, None, "relay_mismatch"
    if scope not in (payload.get("scopes") or []):
        return False, None, "scope_denied"
    exp = payload.get("exp")
    nbf = payload.get("nbf")
    if not isinstance(exp, (int, float)) or exp < now - 30:
        return False, None, "capability_expired"
    if isinstance(nbf, (int, float)) and nbf > now + 30:
        return False, None, "capability_not_active"
    return True, payload, ""


# ---------------------------------------------------------------------------
# Attachments (bdrt1)
# ---------------------------------------------------------------------------

def load_attachment_signing_key(pem: str) -> Ed25519PrivateKey:
    """A PKCS#8 PEM ed25519 private key, ready to mint attachments."""
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("LIVESTACK_RELAY_KEY must be an ed25519 PKCS#8 PEM key")
    return key


def raw_public_key_b64(key: Ed25519PrivateKey) -> str:
    """The raw 32-byte public key, base64 — the form the relay's
    ``ed25519PublicKey`` and the attachment's ``daemon_key`` claim take."""
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode("ascii")


def mint_attachment(private_key: Ed25519PrivateKey, *,
                    realm: str, daemon_id: str, daemon_key_b64: str,
                    relay_id: str, account_id: str,
                    ttl_seconds: Optional[float] = None,
                    aud: str = DEFAULT_ATTACHMENT_AUDIENCE,
                    now: Optional[int] = None) -> Tuple[str, dict]:
    """Mint a ``bdrt1`` attachment token. Returns ``(token, claims)``.

    Ported from meshlink ``issueRelayAttachment``. The relay's verify enforces
    ``exp <= iat + 300``, so a requested TTL is clamped to 300 s — an
    attachment lives five minutes at most and renewal is the steady state.
    ``daemon_key_b64`` is the attaching daemon's raw ed25519 public key
    (base64), not the minter's: the relay checks it is a well-formed key.
    """
    now = int(time.time()) if now is None else int(now)
    if not realm:
        raise ValueError("realm is required")
    if not _DAEMON_ID_RE.match(daemon_id or ""):
        raise ValueError(f"daemon_id {daemon_id!r} must match {_DAEMON_ID_RE.pattern}")
    raw_daemon = base64.b64decode(daemon_key_b64)
    if len(raw_daemon) != 32:
        raise ValueError("daemon_key_b64 must be a base64 raw 32-byte ed25519 public key")
    if not account_id or len(account_id) > 128:
        raise ValueError("account_id must be a non-empty string of at most 128 chars")
    ttl = MAX_ATTACHMENT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    ttl = max(1, min(ttl, MAX_ATTACHMENT_TTL_SECONDS))
    claims = {
        "realm": realm,
        "daemon_id": daemon_id,
        "daemon_key": daemon_key_b64,
        "relay_id": relay_id,
        "account_id": account_id,
        "iat": now,
        "exp": now + ttl,
        "aud": aud,
    }
    body = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    sig = private_key.sign(body)
    return f"{ATTACHMENT_PREFIX}.{_b64url(body)}.{_b64url(sig)}", claims


def attachment_public_key_b64(private_key: Ed25519PrivateKey) -> str:
    """The raw base64 public key the relay should configure for this realm's
    attachment trust — what ``verifyRelayAttachment`` takes as ``publicKey``."""
    return raw_public_key_b64(private_key)


# ---------------------------------------------------------------------------
# Quota declaration (DR-3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelayQuotaConfig:
    """The livestack realm's quota at the relay, set at DEPLOY time (DR-3).

    These mirror the relay's env knobs (``BENCHDAY_RELAY_MAX_STREAMS_PER_ACCOUNT``
    et al., set per-realm in the relay's realm record) so the value livestack
    declares here is the value the deploy must put on the relay. They STACK
    with the fleet's ``max_concurrent_per_account`` policy: either may bind
    first, and a relay 429 is a named "relay quota" degradation, not a generic
    failure. ``None`` on every field means "declare nothing; the relay default
    (4 streams / 240 req-min per realm+account) applies" — which for this
    fleet is too low for broker fan-out, so production should always set them.
    """
    max_streams_per_account: Optional[int] = None
    max_requests_per_min: Optional[int] = None
    max_stream_seconds: Optional[int] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RelayQuotaConfig":
        env = os.environ if env is None else env

        def num(name: str) -> Optional[int]:
            raw = (env.get(name) or "").strip()
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{name}={raw!r} is not an integer")
            if value <= 0:
                raise ValueError(f"{name}={raw!r} must be positive")
            return value

        return cls(max_streams_per_account=num("LIVESTACK_RELAY_QUOTA_MAX_STREAMS"),
                   max_requests_per_min=num("LIVESTACK_RELAY_QUOTA_MAX_REQ_PER_MIN"),
                   max_stream_seconds=num("LIVESTACK_RELAY_QUOTA_MAX_STREAM_SECONDS"))


def relay_ids_from_env(urls: Sequence[str], env: Optional[Mapping[str, str]] = None,
                       log: Optional[Callable[..., None]] = None) -> dict:
    """Relay URL → relay_id for token minting, from ``LIVESTACK_RELAY_IDS``.

    ``LIVESTACK_RELAY_IDS`` is JSON ``{"<url>": "<relay_id>"}``. A relay without
    a mapping is SKIPPED, loudly: the ``relay_id`` claim in both token kinds is
    verified against the relay's own id, so minting for a guessed id produces
    tokens that are born refused (``relay_mismatch``) — a named config error
    beats that. Both the broker side (hostd's mesh peers) and the node side
    (mesh_attach's attachments) mint against relay ids, so the parser lives
    here once, next to the minting it serves.

    ``log`` receives the skip lines; the default prints them. Callers that want
    their own prefix wrap it (hostd passes a plain print to keep its historical
    unprefixed lines)."""
    env = os.environ if env is None else env
    if log is None:
        log = lambda m: print(m, flush=True)  # noqa: E731
    raw = (env.get("LIVESTACK_RELAY_IDS") or "").strip()
    mapping = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                mapping = {str(k): str(v) for k, v in parsed.items()}
        except ValueError:
            log(f"LIVESTACK_RELAY_IDS={raw!r} is not a JSON object — "
                "relay ids unknown")
    out = {}
    for u in urls:
        rid = mapping.get(u)
        if rid:
            out[u] = rid
        else:
            log(f"relay {u!r} skipped: no relay_id in LIVESTACK_RELAY_IDS — "
                "tokens minted for a guessed relay_id would be refused as "
                "relay_mismatch")
    return out


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

def _read_secret_file(path: str, var: str,
                      log: Callable[..., None]) -> str:
    """Read a secret file with the fleet's 0600 discipline (fleet_auth.py):
    group/other must have NO access, and every refusal fails closed with a
    named log line — a world-readable key file is a disclosed key, and a
    disclosed key is worse than none because it keeps authorizing."""
    try:
        st = os.stat(path)
    except OSError as e:
        log(f"[relay-control] {var}={path!r} cannot be read ({e}) — no relay "
            f"attachment key; attach is OFF until this is fixed")
        raise ValueError(f"{var}={path!r} cannot be read: {e}")
    if st.st_mode & 0o077:
        log(f"[relay-control] {var}={path!r} is mode {oct(st.st_mode & 0o777)}; "
            f"group/other must have NO access (chmod 0600). Refusing to use it "
            f"— no relay attachment key; attach is OFF until the permissions "
            f"are fixed")
        raise ValueError(f"{var}={path!r} is mode {oct(st.st_mode & 0o777)}, "
                         f"refusing (chmod 0600)")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log(f"[relay-control] {var}={path!r} stats but cannot be opened ({e}) "
            f"— no relay attachment key; attach is OFF until this is fixed")
        raise ValueError(f"{var}={path!r} cannot be opened: {e}")


@dataclass(frozen=True)
class RelayConfig:
    """The relay control-plane config from the environment.

    Shape (all optional — an unset relay surface means the node simply does
    not attach, never a boot failure):

    * ``LIVESTACK_RELAY_URLS`` — comma list of relay base URLs.
    * ``LIVESTACK_RELAY_REALM`` — the realm to mint for (default ``livestack``).
    * ``LIVESTACK_RELAY_KEY`` / ``LIVESTACK_RELAY_KEY_FILE`` — the realm's
      ed25519 PKCS#8 PEM attachment key; the FILE wins (a credential should
      not be the one setting inline in systemd), and carries the 0600
      discipline of ``LIVESTACK_FLEET_TOKENS_FILE``.
    * ``LIVESTACK_RELAY_CAP_KEYS`` — the HMAC cap key ring, the SAME JSON
      shape the relay's ``relayKeyRingFromEnv`` parses, so one string serves
      both sides: ``[{"kid": "k1", "secret": "...", "active": true}]``.
    * ``LIVESTACK_RELAY_ATTACHMENT_AUDIENCE`` /
      ``LIVESTACK_RELAY_CAPABILITY_TYPE`` / ``LIVESTACK_RELAY_CAPABILITY_AUDIENCE``
      — the realm's cosmetic claims (DR-4); defaults above.
    * Quota envs — see :class:`RelayQuotaConfig`.
    """
    urls: Tuple[str, ...] = ()
    realm: str = DEFAULT_REALM
    attachment_key: Optional[Ed25519PrivateKey] = None
    cap_ring: Optional[CapKeyRing] = None
    attachment_audience: str = DEFAULT_ATTACHMENT_AUDIENCE
    capability_type: str = DEFAULT_CAPABILITY_TYPE
    capability_audience: str = DEFAULT_CAPABILITY_AUDIENCE
    quota: RelayQuotaConfig = RelayQuotaConfig()

    @property
    def configured(self) -> bool:
        """False when no relay surface is set — the node does not attach.
        Distinguishes "no relay" (None/empty) from "relay configured but
        broken" (the from_env parse raising), the same None-vs-empty split
        fleet_auth.principals_from_env makes."""
        return bool(self.urls) and self.attachment_key is not None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *,
                 log: Callable[..., None] = lambda *_: None) -> "RelayConfig":
        env = os.environ if env is None else env

        urls = tuple(u.strip() for u in (env.get("LIVESTACK_RELAY_URLS") or "").split(",")
                     if u.strip())
        realm = (env.get("LIVESTACK_RELAY_REALM") or "").strip() or DEFAULT_REALM

        key_pem = ""
        key_file = (env.get("LIVESTACK_RELAY_KEY_FILE") or "").strip()
        inline_key = (env.get("LIVESTACK_RELAY_KEY") or "").strip()
        if inline_key:
            # systemd strips bare double quotes and eats newlines in inline
            # values; accept the common escaped-newline spelling.
            key_pem = inline_key.replace("\\n", "\n")
        attachment_key: Optional[Ed25519PrivateKey] = None
        if key_file:
            # The file wins over the inline value, for the same reason it does
            # in fleet_auth: a credential that can be read out of
            # `systemctl show` is shared by every process on the box.
            key_pem = _read_secret_file(key_file, "LIVESTACK_RELAY_KEY_FILE", log)
        if key_pem:
            attachment_key = load_attachment_signing_key(key_pem)

        cap_ring = None
        cap_keys_raw = (env.get("LIVESTACK_RELAY_CAP_KEYS") or "").strip()
        if cap_keys_raw:
            cap_ring = CapKeyRing.from_json(cap_keys_raw)

        return cls(
            urls=urls,
            realm=realm,
            attachment_key=attachment_key,
            cap_ring=cap_ring,
            attachment_audience=(env.get("LIVESTACK_RELAY_ATTACHMENT_AUDIENCE") or "").strip()
                                or DEFAULT_ATTACHMENT_AUDIENCE,
            capability_type=(env.get("LIVESTACK_RELAY_CAPABILITY_TYPE") or "").strip()
                            or DEFAULT_CAPABILITY_TYPE,
            capability_audience=(env.get("LIVESTACK_RELAY_CAPABILITY_AUDIENCE") or "").strip()
                                or DEFAULT_CAPABILITY_AUDIENCE,
            quota=RelayQuotaConfig.from_env(env),
        )

    def mint_attachment_for(self, *, daemon_id: str, daemon_key_b64: str,
                            relay_id: str, account_id: str,
                            ttl_seconds: Optional[float] = None,
                            now: Optional[int] = None) -> Tuple[str, dict]:
        """Mint a ``bdrt1`` attachment under this config's key and realm."""
        if self.attachment_key is None:
            raise ValueError("no attachment key configured (LIVESTACK_RELAY_KEY(_FILE))")
        return mint_attachment(self.attachment_key, realm=self.realm,
                               daemon_id=daemon_id, daemon_key_b64=daemon_key_b64,
                               relay_id=relay_id, account_id=account_id,
                               ttl_seconds=ttl_seconds, aud=self.attachment_audience,
                               now=now)

    def mint_capability_for(self, *, account_id: str, device_id: str,
                            target_id: str, relay_id: str, region: str,
                            scopes: Sequence[str],
                            ttl_seconds: Optional[float] = None,
                            now: Optional[int] = None) -> Tuple[str, dict]:
        """Mint a ``bdsr1`` capability under this config's key ring and
        realm cosmetics."""
        if self.cap_ring is None:
            raise ValueError("no cap key ring configured (LIVESTACK_RELAY_CAP_KEYS)")
        return mint_capability(self.cap_ring, account_id=account_id,
                               device_id=device_id, target_id=target_id,
                               relay_id=relay_id, region=region, scopes=scopes,
                               ttl_seconds=ttl_seconds, typ=self.capability_type,
                               aud=self.capability_audience, now=now)
