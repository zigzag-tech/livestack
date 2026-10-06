"""Schema for the workload authority's config file (no environment variables).

Unknown or invalid keys fail closed at startup and on SIGHUP reload. Sections
validated elsewhere at construction (principals -> http.Principal, limits ->
model.Limits, github_remote, compilation_*) are only
type-checked here so there is one owner for each rule.
"""
from pathlib import Path
from typing import Any, Optional, Union

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, field_validator


class HandlerReleasePolicy(BaseModel):
    """The handler release registry's policy section. Unknown keys fail closed. Per-handler
    entries (runtimes/backends) are judged by HandlerReleaseRegistry at construction, which
    stays the one owner of those rules; the burst age floor is judged here too so a bad
    value never reaches the registry."""
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: Optional[StrictStr] = None
    retention_seconds: Optional[StrictInt] = Field(default=None, ge=86400)
    # Minimum age before capacity-driven eviction may reclaim an UNREFERENCED release.
    # Unset disables eviction (a full registry refuses by name). Floor: one hour.
    burst_min_age_seconds: Optional[StrictInt] = Field(default=None, ge=3600)
    handlers: Optional[dict[str, dict[str, Any]]] = None


class ReloadableConfig(BaseModel):
    """What SIGHUP re-reads (docs/authority-principal-reload.md): principals, the installed
    handler ids (add-only) and the handler release policy. The rest of the file is the
    startup gate's business, so it is not re-judged."""
    model_config = ConfigDict(extra='ignore', strict=True)
    principals: list[dict[str, Any]]
    handlers: list[StrictStr]
    handler_release_policy: Optional[HandlerReleasePolicy] = None


class AuthorityConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    state_dir: StrictStr = Field(min_length=1)
    handlers: list[StrictStr]
    principals: list[dict[str, Any]]
    bind: StrictStr = Field(default='127.0.0.1', min_length=1)
    port: StrictInt = Field(default=8802, ge=0, le=65535)
    # Origin used in upload_url returned by POST /upload-grants, for holders that
    # reach the authority by a different address than the Host they were given.
    public_base_url: Optional[StrictStr] = None
    limits: Optional[dict[str, Any]] = None
    blob_limits: Optional[dict[str, Any]] = None
    environment_handlers: Optional[dict[str, Any]] = None
    compilation_handlers: Optional[dict[str, Any]] = None
    compilation_policy: Optional[StrictStr] = None  # path to the policy file
    github_remote: Optional[dict[str, Any]] = None
    artifact_mirror: Optional[dict[str, Any]] = None
    handler_release_policy: Optional[HandlerReleasePolicy] = None

    @field_validator('public_base_url')
    @classmethod
    def _origin_only(cls, value):
        if value is None:
            return value
        url = urlparse(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.path not in ('', '/')
                or url.params or url.query or url.fragment or url.username or url.password):
            raise ValueError('public_base_url must be an http(s) origin without path, query or credentials')
        return value.rstrip('/')

    @field_validator('blob_limits')
    @classmethod
    def _blob_keys(cls, value):
        allowed = {'max_bytes', 'max_object_bytes', 'max_objects', 'retention_seconds'}
        if value is not None:
            unknown = set(value) - allowed
            if unknown:
                raise ValueError(f'unknown blob_limits keys: {sorted(unknown)}')
            for key, number in value.items():
                if number is None and key == 'retention_seconds':
                    continue
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    raise ValueError(f'blob_limits.{key} must be a positive integer')
        return value


def load_config(path, schema=AuthorityConfig):
    """Parse and validate the config file; raises ValueError (pydantic's
    ValidationError is one) without echoing values, so no token leaks."""
    try:
        raw = Path(path).read_text()
    except OSError as exc:
        raise ValueError(f'config unreadable: {type(exc).__name__}') from exc
    try:
        return schema.model_validate_json(raw).model_dump(exclude_unset=True)
    except ValueError as exc:
        # Locations and messages only: never the input values (principal tokens).
        problems = getattr(exc, 'errors', lambda **_: [])(include_input=False, include_url=False)
        raise ValueError('invalid authority config: ' + '; '.join(
            f"{'.'.join(map(str, p['loc'])) or '<file>'}: {p['msg']}" for p in problems)) from None
