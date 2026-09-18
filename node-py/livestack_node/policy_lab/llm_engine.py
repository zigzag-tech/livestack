"""Deterministic aggregate LLM prefill/decode and prefix-cache model."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .contracts import ContractError


class LLMEngineError(ContractError):
    pass


@dataclass(frozen=True)
class PrefixCacheKey:
    model_revision: str
    runtime_revision: str
    tokenizer_revision: str
    tenant_scope: str
    prefix_id: str


@dataclass(frozen=True)
class LLMRequest:
    request_id: str
    model_revision: str
    runtime_revision: str
    tokenizer_revision: str
    tenant_scope: str
    prompt_tokens: int
    output_tokens: int
    prefix_id: str | None
    prefix_tokens: int
    batching_eligible: bool


@dataclass(frozen=True)
class PrefillResult:
    request_id: str
    started_at_us: int
    finished_at_us: int
    cache_hit_tokens: int
    uncached_tokens: int


@dataclass(frozen=True)
class DecodeStep:
    request_ids: tuple[str, ...]
    started_at_us: int
    finished_at_us: int


@dataclass
class _Active:
    request: LLMRequest
    generated_tokens: int = 0


class LLMEngine:
    unsupported_effects = (
        "continuous_batch_reordering",
        "speculative_acceptance",
        "engine_scheduler_microtiming",
    )

    def __init__(
        self,
        context_limit: int,
        prefill_us_per_token: int,
        decode_step_us_by_batch: dict[int, int],
        kv_bytes_per_token: int,
        prefix_cache_capacity_tokens: int,
    ) -> None:
        if min(context_limit, prefill_us_per_token, kv_bytes_per_token) <= 0:
            raise LLMEngineError("LLM engine parameters must be positive")
        if prefix_cache_capacity_tokens < 0 or not decode_step_us_by_batch:
            raise LLMEngineError("invalid cache or decode profile")
        self.context_limit = context_limit
        self.prefill_us_per_token = prefill_us_per_token
        self.decode_step_us_by_batch = dict(decode_step_us_by_batch)
        self.kv_bytes_per_token = kv_bytes_per_token
        self.prefix_cache_capacity_tokens = prefix_cache_capacity_tokens
        self._prefixes: OrderedDict[PrefixCacheKey, int] = OrderedDict()
        self._active: dict[str, _Active] = {}

    def put_prefix(self, key: PrefixCacheKey, token_count: int) -> None:
        if token_count <= 0 or token_count > self.prefix_cache_capacity_tokens:
            raise LLMEngineError("prefix does not fit cache")
        self._prefixes.pop(key, None)
        self._prefixes[key] = token_count
        while sum(self._prefixes.values()) > self.prefix_cache_capacity_tokens:
            self._prefixes.popitem(last=False)

    def _cache_hit(self, request: LLMRequest) -> int:
        if request.prefix_id is None:
            return 0
        key = PrefixCacheKey(
            request.model_revision,
            request.runtime_revision,
            request.tokenizer_revision,
            request.tenant_scope,
            request.prefix_id,
        )
        cached = self._prefixes.get(key, 0)
        if cached:
            self._prefixes.move_to_end(key)
        return min(cached, request.prefix_tokens, request.prompt_tokens)

    def prefill(self, request: LLMRequest, *, at_us: int) -> PrefillResult:
        if request.request_id in self._active:
            raise LLMEngineError("duplicate active LLM request")
        if request.prompt_tokens + request.output_tokens > self.context_limit:
            raise LLMEngineError("request exceeds context limit")
        if min(request.prompt_tokens, request.output_tokens, request.prefix_tokens, at_us) < 0:
            raise LLMEngineError("request values must be nonnegative")
        hit = self._cache_hit(request)
        uncached = request.prompt_tokens - hit
        result = PrefillResult(
            request.request_id,
            at_us,
            at_us + uncached * self.prefill_us_per_token,
            hit,
            uncached,
        )
        self._active[request.request_id] = _Active(request)
        return result

    def decode_step(self, request_ids: list[str], *, at_us: int) -> DecodeStep:
        if not request_ids or len(set(request_ids)) != len(request_ids):
            raise LLMEngineError("decode batch must contain unique requests")
        active = [self._active[request_id] for request_id in request_ids]
        reference = active[0].request
        if any(
            not item.request.batching_eligible
            or item.request.model_revision != reference.model_revision
            or item.request.runtime_revision != reference.runtime_revision
            for item in active
        ):
            raise LLMEngineError("decode batch contains incompatible requests")
        duration = self.decode_step_us_by_batch.get(len(active))
        if duration is None:
            raise LLMEngineError("decode batch size lacks a measured profile")
        for item in active:
            if item.generated_tokens >= item.request.output_tokens:
                raise LLMEngineError("request has no remaining decode work")
            item.generated_tokens += 1
        return DecodeStep(tuple(request_ids), at_us, at_us + duration)

    def kv_bytes(self, request_id: str) -> int:
        active = self._active[request_id]
        return (
            active.request.prompt_tokens + active.generated_tokens
        ) * self.kv_bytes_per_token
