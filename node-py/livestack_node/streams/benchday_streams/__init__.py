# VENDORED UNCHANGED from benchday packages/benchday-plugin-api/sdk/python/benchday_streams/__init__.py
# benchday commit b0d39ba3b (branch agent/services-own-streams, services-own-their-streams 1.2/1.5);
# not yet on benchday origin/main when vendored (2026-10-09). Re-vendor by copying the file below this header.
# Standard library only. Do not edit here; fix upstream in benchday.
"""Benchday local-ingress producer SDK (Python, standard library only).

Speaks the same frames as sdk/typescript/ingress-client.ts. Harmony's hostd (FastAPI over the Rust
residency core) consumes it exactly as a third-party plugin would; it has no service-specific path.
"""
from .client import (  # noqa: F401
    IngressClient, IngressRefusal, ProducerDisconnected, ReconnectingProducer,
    WriteFrame, ingress_socket_path, intent_partial, reference,
)
