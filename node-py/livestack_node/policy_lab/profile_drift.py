"""Domain-scoped immutable calibration certificate identity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationCertificate:
    domain_id: str
    profile_sha256: str
    evaluator_sha256: str
    dataset_sha256: str


def certificate_applies(
    certificate: CalibrationCertificate, *, domain_id: str, profile_sha256: str
) -> bool:
    return certificate.domain_id == domain_id and certificate.profile_sha256 == profile_sha256
