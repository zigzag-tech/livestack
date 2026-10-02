"""klein runtime: the parts that run without a GPU. The GPU path is verified on
zz-joe by a real generation (deploy record in examples/harmony-image/README.md)."""
from types import SimpleNamespace

import pytest

from livestack_node.imagegen import klein


class Vae:
    config = SimpleNamespace(block_out_channels=[128, 256, 512, 512])

    def __init__(self):
        self.calls = []

    def enable_tiling(self):
        self.calls.append("tiling")

    def enable_slicing(self):
        self.calls.append("slicing")


def test_vae_tiles_below_the_768px_the_2070_is_qualified_for():
    # diffusers' default 1024 px threshold decoded 768 px images whole and OOMed.
    vae = Vae()
    klein.configure_vae_tiling(vae)
    assert vae.calls == ["tiling", "slicing"]
    assert vae.tile_sample_min_size == 512 < 768
    assert vae.tile_latent_min_size == 64


@pytest.mark.parametrize("model,steps,quant", [
    ("black-forest-labs/FLUX.2-klein-9B", 4, "nf4"),
    (klein.MODEL, 28, "nf4"),
    (klein.MODEL, 4, "int8"),
])
def test_runtime_refuses_configurations_it_was_not_qualified_for(model, steps, quant):
    with pytest.raises(ValueError, match="Klein 4B"):
        klein.KleinRuntime("/nonexistent", model, steps=steps, quantization=quant)
