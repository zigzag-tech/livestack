"""Real GGUF tensors reproduce the timestep storage-dtype regression on CPU."""
from types import SimpleNamespace
import pytest

torch = pytest.importorskip("torch")
gguf = pytest.importorskip("gguf")
pytest.importorskip("sensenova_u1")
from diffusers.quantizers.gguf.utils import GGUFLinear, GGUFParameter, dequantize_gguf_tensor
from sensenova_u1.models.neo_unify.modeling_fm_modules import TimestepEmbedder
from livestack_node.imagegen.sensenova import restore_timestep_dtype


@pytest.mark.parametrize("key", ["timestep_embedder", "noise_scale_embedder"])
def test_quantized_timestep_anchor_uses_compute_dtype_and_preserves_weights(key):
    torch.manual_seed(7)
    embedder = TimestepEmbedder(32, frequency_embedding_size=32).to(torch.bfloat16)
    original = embedder.mlp[0]
    packed = gguf.quants.quantize(original.weight.float().detach().numpy(), gguf.GGMLQuantizationType.Q8_0)
    quantized = GGUFLinear(32, 32, bias=True, compute_dtype=torch.bfloat16)
    quantized.weight = GGUFParameter(torch.from_numpy(packed), quant_type=gguf.GGMLQuantizationType.Q8_0)
    quantized.bias = original.bias
    expected_weight = dequantize_gguf_tensor(quantized.weight).to(torch.bfloat16)
    embedder.mlp[0] = quantized
    model = SimpleNamespace(fm_modules={key: embedder})
    restore_timestep_dtype(model)
    # Before the fix, forward converts sinusoidal inputs to uint8 because it
    # reads .weight.dtype, then F.linear raises Byte vs BFloat16.
    result = embedder(torch.tensor([0.5]))
    assert result.dtype == torch.bfloat16 and torch.isfinite(result).all()
    torch.testing.assert_close(embedder.mlp[0].weight, expected_weight)
    assert embedder.mlp[0].bias is original.bias


def test_official_bf16_override_restores_quantized_timestep(tmp_path):
    from safetensors.torch import save_file
    from livestack_node.imagegen.sensenova import restore_bf16_modules
    embedder = TimestepEmbedder(32, frequency_embedding_size=32).to(torch.bfloat16)
    expected = {f'fm_modules.timestep_embedder.{k}': v.detach().clone()
                for k, v in embedder.state_dict().items()}
    model = torch.nn.Module()
    model.fm_modules = torch.nn.ModuleDict({'timestep_embedder': embedder})
    packed = gguf.quants.quantize(embedder.mlp[0].weight.float().detach().numpy(), gguf.GGMLQuantizationType.Q8_0)
    quantized = GGUFLinear(32, 32, bias=True, compute_dtype=torch.bfloat16)
    quantized.weight = GGUFParameter(torch.from_numpy(packed), quant_type=gguf.GGMLQuantizationType.Q8_0)
    quantized.bias = embedder.mlp[0].bias
    embedder.mlp[0] = quantized
    checkpoint = tmp_path / 'fm.safetensors'
    save_file(expected, str(checkpoint))
    restore_bf16_modules(model, str(checkpoint))
    for name, weight in expected.items():
        torch.testing.assert_close(model.state_dict()[name], weight, rtol=0, atol=0)
    assert torch.isfinite(embedder(torch.tensor([0.5]))).all()
