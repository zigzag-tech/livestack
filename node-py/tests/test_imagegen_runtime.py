"""Regression for RTX 2070 FP16 overflow returning an all-black PNG."""
import base64
import builtins
import hashlib
import io
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from livestack_node.imagegen import runtime


CONTROLNET_REVISION = "7" * 40
CONTROLNET_CONFIG_SHA256 = "8" * 64


class _Tensor:
    def __init__(self, name):
        self.name = name
        self.moves = []

    def to(self, *args, **kwargs):
        self.moves.append((args, kwargs))
        return self

    def __add__(self, other):
        self.added = other
        return self


class _Generator:
    def __init__(self, device):
        self.device = device
        self.seed = None

    def manual_seed(self, seed):
        self.seed = seed
        return self


def _nonblank(size):
    return Image.linear_gradient("L").resize(size).convert("RGB")


def _guided_request():
    stream = io.BytesIO()
    Image.new("L", (768, 432), 128).save(stream, format="PNG")
    data = stream.getvalue()
    return {
        "prompt": "A grounded landscape",
        "width": 768,
        "height": 432,
        "seed": 1863,
        "control_image": {
            "media_type": "image/png",
            "b64_json": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "controlnet_conditioning_scale": 0.8,
        "harmony_requires": {
            "class": "imagegen", "task": "text_to_image", "depth_guide": True,
            "model": "Qwen/Qwen-Image-2512", "revision": "6" * 40,
            "controlnet_model": "test/Qwen-ControlNet",
            "controlnet_revision": CONTROLNET_REVISION,
            "controlnet_config_sha256": CONTROLNET_CONFIG_SHA256,
            "quantization": "nf4",
        },
    }


def _pipe(env, transformer):
    pipe = Mock()
    pipe.scheduler = object()
    pipe.vae = Mock()
    pipe.vae.dtype = "torch.bfloat16"
    pipe.text_encoder = Mock()
    pipe.tokenizer = object()
    pipe.transformer = transformer
    pipe.encode_prompt.side_effect = (
        lambda prompt, **kwargs: (_Tensor(prompt), _Tensor(f"mask:{prompt}")))

    def generate(**kwargs):
        size = env.output_size or (kwargs["width"], kwargs["height"])
        image = Image.new("RGB", size) if env.blank else _nonblank(size)
        return SimpleNamespace(images=[image])

    pipe.side_effect = generate
    return pipe


def _model_stack(monkeypatch):
    env = SimpleNamespace(output_size=None, blank=False)
    env.depth_config = {
        "model": "test/Qwen-ControlNet", "revision": CONTROLNET_REVISION,
        "snapshot": "control-snapshot", "config_sha256": CONTROLNET_CONFIG_SHA256,
    }
    env.transformer = Mock()
    env.transformer.dtype = "torch.bfloat16"
    env.text_pipe = _pipe(env, env.transformer)
    env.depth_pipe = _pipe(env, env.transformer)
    for name in ("scheduler", "vae", "text_encoder", "tokenizer", "transformer"):
        setattr(env.depth_pipe, name, getattr(env.text_pipe, name))
    env.controlnet = Mock()

    env.torch = ModuleType("torch")
    env.torch.bfloat16 = "torch.bfloat16"
    env.torch.float32 = "torch.float32"
    env.torch.set_num_threads = Mock()
    env.torch.device = lambda name: name
    env.torch.cuda = Mock()
    env.torch.cuda.max_memory_allocated.return_value = 123
    env.torch.cuda.max_memory_reserved.return_value = 456
    env.torch.inference_mode = lambda: nullcontext()
    env.torch.Generator = _Generator

    env.diffusers = ModuleType("diffusers")
    env.diffusers.BitsAndBytesConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    env.transformer_class = Mock()
    env.transformer_class.from_pretrained.return_value = env.transformer
    env.text_pipeline_class = Mock()
    env.text_pipeline_class.from_pretrained.return_value = env.text_pipe
    env.controlnet_class = Mock()
    env.controlnet_class.from_pretrained.return_value = env.controlnet
    env.control_pipeline_class = Mock()
    env.control_pipeline_class.return_value = env.depth_pipe
    env.diffusers.QwenImageTransformer2DModel = env.transformer_class
    env.diffusers.QwenImagePipeline = env.text_pipeline_class
    env.diffusers.QwenImageControlNetModel = env.controlnet_class
    env.diffusers.QwenImageControlNetPipeline = env.control_pipeline_class
    env.diffusers.ZImageTransformer2DModel = Mock()
    env.diffusers.ZImagePipeline = Mock()
    monkeypatch.setitem(sys.modules, "torch", env.torch)
    monkeypatch.setitem(sys.modules, "diffusers", env.diffusers)
    return env


def _depth_runtime(monkeypatch, **overrides):
    env = _model_stack(monkeypatch)
    options = dict(
        steps=50, unit="qwen_image_2512_depth_nf4", quantization="nf4",
        depth_guide=env.depth_config,
    )
    options.update(overrides)
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", **options)
    return instance, env


def _legacy_runtime(monkeypatch):
    env = _model_stack(monkeypatch)
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=20)
    return instance, env


def _refuse_model_imports(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "diffusers"}:
            raise AssertionError(f"model loading began with {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_black_output_is_refused_instead_of_reported_as_success():
    with pytest.raises(RuntimeError, match="blank"):
        runtime.validate_image(Image.new("RGB", (512, 512), (0, 0, 0)))


@pytest.mark.parametrize("model,unit,quantization", [
    ("Qwen/Qwen-Image-2512", "qwen_image_2512_depth_nf4", "nf4"),
    ("Qwen/Qwen-Image-2512", "qwen_image_2512_depth_int8", "int8"),
    ("stabilityai/stable-diffusion-xl-base-1.0", "sdxl_depth_int8", "int8"),
    ("black-forest-labs/FLUX.2-dev", "flux_2_dev_depth_int8", "int8"),
])
def test_exact_depth_unit_quantization_pairs_are_accepted(model, unit, quantization):
    assert runtime.validate_depth_variant(model, unit, quantization) is None


@pytest.mark.parametrize("model,unit,quantization", [
    ("Qwen/Qwen-Image-2512", "qwen_image_2512_depth_nf4", "int8"),
    ("Qwen/Qwen-Image-2512", "qwen_image_2512_depth_int8", "nf4"),
    ("Qwen/Qwen-Image-2512", "qwen_image_2512_depth_nf4", "int4"),
    ("Qwen/Qwen-Image-2512", "qwen_image_2512", "nf4"),
    ("test/not-qwen", "qwen_image_2512_depth_nf4", "nf4"),
])
def test_invalid_depth_variant_is_refused_before_any_model_loading(
        monkeypatch, model, unit, quantization):
    _refuse_model_imports(monkeypatch)
    with pytest.raises(runtime.DepthConfigurationError):
        runtime.ImageRuntime(
            "unused-snapshot", model, steps=1, unit=unit,
            quantization=quantization, depth_guide={})


def test_non_object_depth_config_is_refused_before_any_model_loading(monkeypatch):
    _refuse_model_imports(monkeypatch)
    with pytest.raises(runtime.DepthConfigurationError, match="configuration object"):
        runtime.ImageRuntime(
            "unused-snapshot", "Qwen/Qwen-Image-2512", steps=1,
            unit="qwen_image_2512_depth_nf4", quantization="nf4",
            depth_guide=True)


@pytest.mark.parametrize("quantization", [None, "nf4", "int4", "", 1, True])
def test_invalid_controlnet_quantization_is_refused_before_model_loading(
        monkeypatch, quantization):
    _refuse_model_imports(monkeypatch)
    with pytest.raises(runtime.DepthConfigurationError, match="bf16.*int8"):
        runtime.ImageRuntime(
            "unused-snapshot", "Qwen/Qwen-Image-2512", steps=1,
            unit="qwen_image_2512_depth_int8", quantization="int8",
            depth_guide={"quantization": quantization})


def test_depth_construction_and_guided_call_share_components_and_settings(monkeypatch):
    instance, env = _depth_runtime(
        monkeypatch, true_cfg_scale=4.5, negative_prompt="avoid artifacts",
        max_sequence_length=384)
    assert env.transformer_class.from_pretrained.call_count == 1
    assert env.text_pipeline_class.from_pretrained.call_count == 1
    env.controlnet_class.from_pretrained.assert_called_once_with(
        "control-snapshot", torch_dtype="torch.bfloat16", local_files_only=True)
    env.controlnet.to.assert_called_once_with(
        device="cuda:0", dtype="torch.bfloat16")
    construction = env.control_pipeline_class.call_args.kwargs
    assert construction["scheduler"] is env.text_pipe.scheduler
    assert construction["vae"] is env.text_pipe.vae
    assert construction["text_encoder"] is env.text_pipe.text_encoder
    assert construction["tokenizer"] is env.text_pipe.tokenizer
    assert construction["transformer"] is env.text_pipe.transformer
    assert construction["controlnet"] is env.controlnet
    assert instance.pipe is env.text_pipe
    assert instance.depth_pipe is env.depth_pipe
    assert instance.controlnet is env.controlnet

    request = _guided_request()
    image, metrics = instance.generate(request)
    assert image.size == (768, 432)
    assert env.text_pipe.call_count == 0
    assert env.depth_pipe.call_count == 1
    prompt_calls = env.depth_pipe.encode_prompt.call_args_list
    assert [call.args[0] for call in prompt_calls] == [
        request["prompt"], "avoid artifacts",
    ]
    assert all(call.kwargs["device"] == "cpu" for call in prompt_calls)
    assert all(call.kwargs["max_sequence_length"] == 384 for call in prompt_calls)

    call = env.depth_pipe.call_args.kwargs
    assert call["prompt_embeds"].name == request["prompt"]
    assert call["negative_prompt_embeds"].name == "avoid artifacts"
    assert call["prompt_embeds_mask"].name == f"mask:{request['prompt']}"
    assert call["negative_prompt_embeds_mask"].name == "mask:avoid artifacts"
    assert call["true_cfg_scale"] == 4.5
    assert call["control_image"].mode == "RGB"
    assert call["control_image"].size == (768, 432)
    assert call["controlnet_conditioning_scale"] == 0.8
    assert call["control_guidance_start"] == 0.0
    assert call["control_guidance_end"] == 1.0
    assert call["width"] == 768 and call["height"] == 432
    assert call["num_inference_steps"] == 50
    assert call["generator"].seed == 1863
    assert call["generator"].device == "cuda:0"

    guide_hash = hashlib.sha256(base64.b64decode(
        request["control_image"]["b64_json"], validate=True)).hexdigest()
    assert metrics["depth_guide"] == {
        "image_sha256": guide_hash,
        "conditioning_scale": 0.8,
        "controlnet_model": "test/Qwen-ControlNet",
        "controlnet_revision": CONTROLNET_REVISION,
        "controlnet_config_sha256": CONTROLNET_CONFIG_SHA256,
        "controlnet_quantization": "bf16",
        "control_guidance_start": 0.0,
        "control_guidance_end": 1.0,
    }
    assert metrics["generation_settings"] == {
        "steps": 50,
        "true_cfg_scale": 4.5,
        "negative_prompt": "avoid artifacts",
        "text_encoder": "CPU FP32",
        "max_sequence_length": 384,
    }


def test_int8_controlnet_uses_bnb_cuda_loader_without_bf16_cast(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config)

    env.controlnet_class.from_pretrained.assert_called_once_with(
        "control-snapshot", torch_dtype="torch.bfloat16", local_files_only=True,
        quantization_config=SimpleNamespace(load_in_8bit=True),
        device_map={"": "cuda:0"})
    env.controlnet.to.assert_not_called()
    _, metrics = instance.generate(_guided_request())
    assert metrics["quantization"] == "int8"
    assert metrics["depth_guide"]["controlnet_quantization"] == "int8"


def test_int8_sequential_offload_keeps_controlnet_gpu_resident(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    offload = {"strategy": "sequential", "controlnet_gib": None}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    env.transformer_class.from_pretrained.assert_called_once_with(
        "base-snapshot", subfolder="transformer", torch_dtype="torch.bfloat16",
        quantization_config=SimpleNamespace(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True),
        local_files_only=True, device_map={"": "cpu"})
    env.text_pipe.reset_device_map.assert_called_once_with()
    assert env.text_pipe._exclude_from_cpu_offload == ["vae", "text_encoder"]
    env.text_pipe.enable_sequential_cpu_offload.assert_called_once_with()
    env.controlnet_class.from_pretrained.assert_called_once_with(
        "control-snapshot", torch_dtype="torch.bfloat16", local_files_only=True,
        quantization_config=SimpleNamespace(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True),
        device_map={"": "cuda:0"})
    env.controlnet.to.assert_not_called()
    _, metrics = instance.generate(_guided_request())
    assert metrics["offload"] == offload


def test_int8_sequential_offload_can_keep_vae_on_cpu(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    offload = {"strategy": "sequential", "controlnet_gib": None, "vae": "cpu"}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    assert env.text_pipe._exclude_from_cpu_offload == ["text_encoder"]
    env.text_pipe.enable_sequential_cpu_offload.assert_called_once_with()
    env.text_pipe.vae.to.assert_not_called()
    _, metrics = instance.generate(_guided_request())
    assert metrics["offload"] == offload


def test_int8_sequential_offload_can_include_controlnet(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    env.controlnet._parameters = {}
    offload_fn = Mock()
    monkeypatch.setitem(sys.modules, "accelerate", SimpleNamespace(cpu_offload=offload_fn))
    offload = {"strategy": "sequential", "controlnet_gib": None,
               "controlnet": "sequential", "vae": "cpu"}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    env.controlnet_class.from_pretrained.assert_called_once_with(
        "control-snapshot", torch_dtype="torch.bfloat16", local_files_only=True,
        quantization_config=SimpleNamespace(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True),
        device_map={"": "cpu"})
    offload_fn.assert_called_once_with(env.controlnet, "cuda:0", offload_buffers=False)
    _, metrics = instance.generate(_guided_request())
    assert metrics["offload"] == offload
    assert metrics["offload_map"]["controlnet"] == "sequential:cuda:0"


def test_int8_auto_offload_uses_bounded_transformer_budget(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    env.controlnet._parameters = {}
    env.torch._C = SimpleNamespace(_cuda_clearCublasWorkspaces=Mock())
    offload_fn = Mock()
    monkeypatch.setitem(sys.modules, "accelerate", SimpleNamespace(cpu_offload=offload_fn))
    offload = {"strategy": "auto", "transformer_gib": 1, "controlnet_gib": None,
               "controlnet": "sequential", "vae": "cpu"}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    env.transformer_class.from_pretrained.assert_called_once_with(
        "base-snapshot", subfolder="transformer", torch_dtype="torch.bfloat16",
        quantization_config=SimpleNamespace(load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True),
        local_files_only=True, device_map="auto", max_memory={0: "1GiB", "cpu": "48GiB"},
        offload_folder=instance._offload_tmp.name)
    env.text_pipe.enable_sequential_cpu_offload.assert_not_called()
    offload_fn.assert_called_once_with(env.controlnet, "cuda:0", offload_buffers=False)
    _, metrics = instance.generate(_guided_request())
    assert metrics["offload"] == offload
    env.torch._C._cuda_clearCublasWorkspaces.assert_called_once_with()
    env.torch.cuda.empty_cache.assert_called_once_with()
    temporary = instance._offload_tmp
    temporary.cleanup = Mock()
    instance.close()
    temporary.cleanup.assert_called_once_with()


def test_int8_auto_offload_spreads_transformer_and_controlnet_across_gpus(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    env.controlnet._parameters = {}
    env.transformer.hf_device_map = {"block0": 0, "block1": 1}
    env.transformer.transformer_blocks = [Mock(), Mock()]
    offload_fn = Mock()
    monkeypatch.setitem(sys.modules, "accelerate", SimpleNamespace(cpu_offload=offload_fn))
    offload = {"strategy": "auto", "transformer_gib": {"0": 2, "1": 4},
               "cpu_gib": 48, "controlnet_gib": None,
               "controlnet": "sequential", "controlnet_device": 1}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    assert env.transformer_class.from_pretrained.call_args.kwargs["max_memory"] == {
        0: "2GiB", 1: "4GiB", "cpu": "48GiB"}
    offload_fn.assert_called_once_with(env.controlnet, "cuda:1", offload_buffers=False)
    _, metrics = instance.generate(_guided_request())
    assert metrics["offload_map"]["controlnet"] == "sequential:cuda:1"
    assert set(metrics["device_peaks"]) == {"cuda:0", "cuda:1"}
    hidden = _Tensor("hidden")
    hidden.device = "cuda:0"
    residual = _Tensor("residual")
    hook = env.transformer.transformer_blocks[0].register_forward_hook.call_args.args[0]
    env.transformer._controlnet_active_residuals = [residual]
    encoder = object()
    assert hook(None, None, (encoder, hidden)) == (encoder, hidden)
    assert hidden.added is residual and residual.moves == [(("cuda:0",), {})]
    env.transformer.forward(controlnet_block_samples=[residual])
    assert env.transformer._controlnet_active_residuals is None


def test_single_gpu_transformer_with_controlnet_on_other_gpu_aligns_residuals(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    env.controlnet._parameters = {}
    env.transformer.hf_device_map = {"block0": 0}
    env.transformer.transformer_blocks = [Mock()]
    monkeypatch.setitem(sys.modules, "accelerate", SimpleNamespace(cpu_offload=Mock()))
    runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config,
        offload={"strategy": "auto", "transformer_gib": 2, "controlnet_gib": None,
                 "controlnet": "sequential", "controlnet_device": 1})
    env.transformer.transformer_blocks[0].register_forward_hook.assert_called_once()


def test_cpu_controlnet_moves_only_inputs_and_residuals(monkeypatch):
    env = _model_stack(monkeypatch)
    env.depth_config["quantization"] = "int8"
    env.torch.is_tensor = lambda value: isinstance(value, _Tensor)
    tensor = _Tensor("hidden")
    tensor.device = "cuda:0"
    env.controlnet.forward = Mock(return_value=(tensor,))
    offload = {"strategy": "auto", "transformer_gib": 4, "cpu_gib": 48,
               "controlnet_gib": None, "controlnet": "cpu"}
    instance = runtime.ImageRuntime(
        "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
        unit="qwen_image_2512_depth_int8", quantization="int8",
        depth_guide=env.depth_config, offload=offload)
    output = instance.controlnet.forward(hidden_states=tensor)
    assert output == (tensor,)
    assert tensor.moves == [(("cpu",), {}), (("cuda:0",), {})]
    assert env.controlnet._original_forward.call_args.kwargs["hidden_states"] is tensor
    assert instance.offload_map["controlnet"] == "cpu:cuda:0"


def test_invalid_offload_is_refused_before_model_loading(monkeypatch):
    env = _model_stack(monkeypatch)
    with pytest.raises(runtime.DepthConfigurationError, match="offload"):
        runtime.ImageRuntime(
            "base-snapshot", "Qwen/Qwen-Image-2512", steps=50,
            unit="qwen_image_2512_depth_int8", quantization="int8",
            depth_guide=env.depth_config,
            offload={"strategy": "auto", "controlnet_gib": None})
    env.transformer_class.from_pretrained.assert_not_called()


def test_unguided_request_on_depth_runtime_uses_existing_text_pipeline(monkeypatch):
    instance, env = _depth_runtime(monkeypatch)
    image, metrics = instance.generate({
        "prompt": "ordinary text to image", "width": 512, "height": 512,
        "seed": 9,
    })
    assert image.size == (512, 512)
    assert env.text_pipe.call_count == 1
    assert env.depth_pipe.call_count == 0
    assert metrics["depth_guide"] is None
    assert metrics["generation_settings"]["steps"] == 50
    assert "control_image" not in env.text_pipe.call_args.kwargs
    assert "control_guidance_start" not in env.text_pipe.call_args.kwargs


def test_legacy_runtime_keeps_legacy_generation_metrics(monkeypatch):
    instance, env = _legacy_runtime(monkeypatch)
    _, metrics = instance.generate({
        "prompt": "ordinary text to image", "width": 512, "height": 512,
        "seed": 9,
    })
    assert env.text_pipe.call_args.kwargs["true_cfg_scale"] == 4.0
    assert metrics["depth_guide"] is None
    assert "generation_settings" not in metrics


def test_guided_request_on_unconfigured_runtime_never_invokes_text_pipeline(monkeypatch):
    instance, env = _legacy_runtime(monkeypatch)
    with pytest.raises(RuntimeError, match="not configured"):
        instance.generate(_guided_request())
    assert env.text_pipe.call_count == 0
    assert env.text_pipe.encode_prompt.call_count == 0
    env.torch.cuda.reset_peak_memory_stats.assert_not_called()


@pytest.mark.parametrize("mutation", ["base64", "sha256", "dimensions"])
def test_runtime_revalidates_guide_before_prompt_or_pipeline_execution(monkeypatch, mutation):
    instance, env = _depth_runtime(monkeypatch)
    request = _guided_request()
    if mutation == "base64":
        request["control_image"]["b64_json"] = "not base64!"
    elif mutation == "sha256":
        request["control_image"]["sha256"] = "0" * 64
    else:
        request["width"] = 512
    with pytest.raises(ValueError):
        instance.generate(request)
    assert env.depth_pipe.call_count == 0
    assert env.depth_pipe.encode_prompt.call_count == 0


def test_model_default_output_dimensions_are_refused(monkeypatch):
    instance, env = _depth_runtime(monkeypatch)
    env.output_size = (1024, 1024)
    with pytest.raises(RuntimeError, match="dimensions"):
        instance.generate(_guided_request())
    assert env.depth_pipe.call_count == 1


def test_guided_blank_output_is_still_refused(monkeypatch):
    instance, env = _depth_runtime(monkeypatch)
    env.blank = True
    with pytest.raises(RuntimeError, match="blank"):
        instance.generate(_guided_request())
    assert env.depth_pipe.call_count == 1


def test_close_clears_every_model_reference_before_gc_and_cache_release(monkeypatch):
    instance, env = _depth_runtime(monkeypatch)
    events = []

    def collect():
        events.append(("gc", instance.pipe, instance.depth_pipe, instance.controlnet))

    monkeypatch.setattr(runtime.gc, "collect", collect)
    env.torch.cuda.empty_cache.side_effect = lambda: events.append(
        ("cache", instance.pipe, instance.depth_pipe, instance.controlnet))
    instance.close()
    assert events == [
        ("gc", None, None, None), ("cache", None, None, None),
    ]
    assert instance.pipe is None
    assert instance.depth_pipe is None
    assert instance.controlnet is None
