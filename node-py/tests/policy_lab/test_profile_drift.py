from livestack_node.policy_lab.profile_drift import CalibrationCertificate, certificate_applies


def test_s32_profile_revision_or_domain_change_requires_requalification():
    certificate = CalibrationCertificate(
        domain_id="two-region-llm",
        profile_sha256="a" * 64,
        evaluator_sha256="b" * 64,
        dataset_sha256="c" * 64,
    )
    assert certificate_applies(certificate, domain_id="two-region-llm", profile_sha256="a" * 64)
    assert not certificate_applies(certificate, domain_id="two-region-llm", profile_sha256="d" * 64)
    assert not certificate_applies(certificate, domain_id="other", profile_sha256="a" * 64)
