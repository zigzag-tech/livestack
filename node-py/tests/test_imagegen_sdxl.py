import base64
import hashlib
import io
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from livestack_node.imagegen.sdxl import SDXLImageRuntime


class Tensor:
    def __init__(self, name):
        self.name = name

    def to(self, *args, **kwargs):
        return self


class Generator:
    def __init__(self, device):
        self.device = device
        self.seed = None

    def manual_seed(self, seed):
        self.seed = seed
        return self


def test_sdxl_depth_runtime_uses_int8_unet_and_full_guidance(monkeypatch):
    torch = ModuleType("torch")
    torch.bfloat16 = "bf16"
    torch.float32 = "fp32"
    torch.set_num_threads = Mock()
    torch.device = lambda value: value
    torch.cuda = Mock()
    torch.inference_mode = lambda: nullcontext()
    torch.Generator = Generator
    torch.is_tensor = lambda value: isinstance(value, Tensor)

    unet_class = Mock()
    unet = Mock()
    unet.dtype = "bf16"
    unet_class.from_pretrained.return_value = unet
    control_class = Mock()
    control = Mock()
    control_class.from_pretrained.return_value = control
    pipe = Mock()
    pipe.unet = unet
    pipe.vae.dtype = "fp32"
    pipe.encode_prompt.return_value = tuple(Tensor(name) for name in (
        "prompt", "negative", "pooled", "negative-pooled"))
    pipe.return_value = SimpleNamespace(images=[Image.linear_gradient("L").resize((768, 432)).convert("RGB")])
    pipeline_class = Mock()
    pipeline_class.from_pretrained.return_value = pipe
    diffusers = ModuleType("diffusers")
    diffusers.BitsAndBytesConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    diffusers.UNet2DConditionModel = unet_class
    diffusers.ControlNetModel = control_class
    diffusers.StableDiffusionXLControlNetPipeline = pipeline_class
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)

    instance = SDXLImageRuntime(
        "base", "stabilityai/stable-diffusion-xl-base-1.0", steps=30,
        unit="sdxl_depth_int8", quantization="int8",
        depth_guide={"snapshot": "control", "model": "diffusers/controlnet-depth-sdxl-1.0",
                     "revision": "a" * 40, "config_sha256": "b" * 64})
    unet_class.from_pretrained.assert_called_once_with(
        "base", subfolder="unet", torch_dtype="bf16",
        quantization_config=SimpleNamespace(load_in_8bit=True),
        device_map={"": "cuda:0"}, local_files_only=True)
    pipe.text_encoder.to.assert_called_once_with(device="cpu", dtype="fp32")
    pipe.text_encoder_2.to.assert_called_once_with(device="cpu", dtype="fp32")
    pipe.vae.to.assert_called_once_with(device="cuda:0", dtype="fp32")

    stream = io.BytesIO()
    Image.linear_gradient("L").resize((768, 432)).save(stream, format="PNG")
    raw = stream.getvalue()
    image, metrics = instance.generate({
        "prompt": "a town matching the guide", "width": 768, "height": 432, "seed": 1863,
        "control_image": {"media_type": "image/png", "b64_json": base64.b64encode(raw).decode(),
                          "sha256": hashlib.sha256(raw).hexdigest()},
        "controlnet_conditioning_scale": 0.8,
    })
    assert image.size == (768, 432)
    kwargs = pipe.call_args.kwargs
    assert kwargs["image"].size == (768, 432)
    assert kwargs["controlnet_conditioning_scale"] == 0.8
    assert kwargs["control_guidance_start"] == 0.0
    assert kwargs["control_guidance_end"] == 1.0
    assert kwargs["guidance_scale"] == 7.5
    assert kwargs["generator"].seed == 1863
    assert metrics["generation_settings"] == {
        "steps": 30, "guidance_scale": 7.5,
        "negative_prompt": "blurry, low quality, distorted geometry",
        "text_encoder": "CPU FP32",
    }
    assert metrics["depth_guide"]["controlnet_quantization"] == "bf16"
