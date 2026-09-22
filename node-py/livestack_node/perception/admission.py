"""Pure construction of Harmony perception admission requests."""

LOCATEANYTHING_CUDA_UNIT = "ground_locateanything_nvidia_3b"
LOCATEANYTHING_MLX_UNIT = "ground_locateanything_mlx_3b_8bit"


def broker_admission_payload(request_id: str, owner: str,
                             kind: str = LOCATEANYTHING_CUDA_UNIT) -> dict:
    """Realm scopes ingress authorization; it is never a placement selector."""
    return {
        "id": request_id,
        "kind": kind,
        "owner": owner,
        "owner_asserted": True,
    }
