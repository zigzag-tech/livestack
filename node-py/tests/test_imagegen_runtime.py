"""Regression for RTX 2070 FP16 overflow returning an all-black PNG."""
import pytest
from PIL import Image
from livestack_node.imagegen import runtime


def test_black_output_is_refused_instead_of_reported_as_success():
    with pytest.raises(RuntimeError, match="blank"):
        runtime.validate_image(Image.new("RGB", (512, 512), (0, 0, 0)))
