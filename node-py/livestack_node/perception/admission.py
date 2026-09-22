"""Pure construction of Harmony perception admission requests."""

LOCATEANYTHING_CUDA_UNIT = "ground_locateanything_nvidia_3b"
LOCATEANYTHING_MLX_UNIT = "ground_locateanything_mlx_3b_8bit"
DEEPSEEK_OCR2_UNIT = "recognize_deepseek_ocr2"
HUNYUAN_OCR_UNIT = "recognize_hunyuan_ocr"
GLM_OCR_UNIT = "recognize_glm_ocr"
PADDLEOCR_VL_UNIT = "perceive_paddleocr_vl"
PADDLEOCR_V6_UNIT = "perceive_paddleocr_v6_medium"
SAM21_UNIT = "segment_sam21_small"


def broker_admission_payload(request_id: str, owner: str,
                             kind: str = LOCATEANYTHING_CUDA_UNIT) -> dict:
    """Realm scopes ingress authorization; it is never a placement selector."""
    return {
        "id": request_id,
        "kind": kind,
        "owner": owner,
        "owner_asserted": True,
    }
