"""Diffusers runtime: quantized generator on GPU, text encoder on CPU."""
from __future__ import annotations

import base64
import binascii
import gc
import hashlib
import io
import json
import tempfile
import time

from ..freeing import free_cuda


QWEN_IMAGE_MODEL = "Qwen/Qwen-Image-2512"
SDXL_IMAGE_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
FLUX2_IMAGE_MODEL = "black-forest-labs/FLUX.2-dev"
DEPTH_UNIT_QUANTIZATION = {
    "qwen_image_2512_depth_nf4": "nf4",
    "qwen_image_2512_depth_int8": "int8",
    "sdxl_depth_int8": "int8",
    "flux_2_dev_depth_int8": "int8",
}
CONTROLNET_CLASSES = {
    QWEN_IMAGE_MODEL: "QwenImageControlNetModel",
    SDXL_IMAGE_MODEL: "ControlNetModel",
    FLUX2_IMAGE_MODEL: "Flux2ControlTransformer2DModel",
}


class DepthConfigurationError(ValueError):
    pass


def validate_controlnet_quantization(depth_guide):
    quantization = depth_guide.get("quantization", "bf16")
    if quantization not in ("bf16", "int8"):
        raise DepthConfigurationError(
            f"depth_guide.quantization must be 'bf16' or 'int8', not {quantization!r}")
    return quantization


def _positive_gib(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise DepthConfigurationError(f"{name} must be positive")


def transformer_memory(offload):
    budget = offload["transformer_gib"]
    if isinstance(budget, dict):
        if not budget:
            raise DepthConfigurationError("transformer_gib device budgets must not be empty")
        memory = {}
        for device, value in budget.items():
            if not isinstance(device, str) or not device.isdigit() or str(int(device)) != device:
                raise DepthConfigurationError("transformer_gib device keys must be logical GPU indices")
            _positive_gib(value, f"transformer_gib.{device}")
            memory[int(device)] = f"{value}GiB"
    else:
        _positive_gib(budget, "auto transformer_gib")
        memory = {0: f"{budget}GiB"}
    memory["cpu"] = f"{offload.get('cpu_gib', 48)}GiB"
    return memory


def validate_offload(offload):
    if offload is None:
        return None
    if (not isinstance(offload, dict)
            or not {"strategy", "controlnet_gib"}.issubset(offload)
            or set(offload) - {"strategy", "transformer_gib", "controlnet_gib", "controlnet", "controlnet_device", "vae", "cpu_gib", "trace_memory"}):
        raise DepthConfigurationError("offload has an unsupported shape")
    if offload["strategy"] not in ("sequential", "auto") or offload["controlnet_gib"] is not None:
        raise DepthConfigurationError("offload requires strategy=sequential or auto and controlnet_gib=null")
    if offload["strategy"] == "auto":
        if "transformer_gib" not in offload:
            raise DepthConfigurationError("auto offload requires transformer_gib")
        transformer_memory(offload)
        _positive_gib(offload.get("cpu_gib", 48), "auto cpu_gib")
    elif "transformer_gib" in offload:
        raise DepthConfigurationError("transformer_gib is only valid with auto offload")
    if offload.get("controlnet") not in (None, "sequential", "cpu"):
        raise DepthConfigurationError("offload.controlnet must be sequential or cpu when specified")
    device = offload.get("controlnet_device", 0)
    if type(device) is not int or device < 0:
        raise DepthConfigurationError("offload.controlnet_device must be a nonnegative logical GPU index")
    if offload.get("vae") not in (None, "cpu"):
        raise DepthConfigurationError("offload.vae must be cpu when specified")
    if type(offload.get("trace_memory", False)) is not bool:
        raise DepthConfigurationError("offload.trace_memory must be boolean")
    return dict(offload)


def validate_depth_variant(model, unit, quantization):
    if model not in CONTROLNET_CLASSES:
        raise DepthConfigurationError(f"depth guidance does not support {model}")
    expected = DEPTH_UNIT_QUANTIZATION.get(unit)
    if expected is None:
        raise DepthConfigurationError(f"unsupported depth unit kind: {unit!r}")
    if quantization != expected:
        raise DepthConfigurationError(
            f"depth unit {unit!r} requires quantization {expected!r}, not {quantization!r}")


def validate_image(image):
    from PIL import ImageStat
    if max(ImageStat.Stat(image.convert("RGB")).stddev) < 2:
        raise RuntimeError("image output is nearly blank; refusing a false inference success")


def _decode_control_image(control_image, width, height):
    if not isinstance(control_image, dict) or control_image.get("media_type") != "image/png":
        raise ValueError("control_image must be a JSON PNG object")
    encoded = control_image.get("b64_json")
    if not isinstance(encoded, str):
        raise ValueError("control_image.b64_json must be strict base64")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("control_image.b64_json must be strict base64") from exc
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise ValueError("control_image.b64_json must be strict base64")
    image_sha256 = hashlib.sha256(decoded).hexdigest()
    if control_image.get("sha256") != image_sha256:
        raise ValueError("control_image SHA-256 mismatch")

    from PIL import Image
    try:
        source = Image.open(io.BytesIO(decoded))
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("control_image must be one RGB-readable PNG") from exc
    with source:
        if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
            raise ValueError("control_image must contain exactly one PNG image")
        if source.size != (width, height):
            raise ValueError("control_image dimensions must equal the requested width and height")
        try:
            source.load()
            guide = source.convert("RGB")
            guide.load()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
            raise ValueError("control_image must be one RGB-readable PNG") from exc
    return guide, image_sha256


def _move_tensors(torch, value, device):
    if torch.is_tensor(value):
        return value.to(device)
    if isinstance(value, tuple):
        return tuple(_move_tensors(torch, item, device) for item in value)
    if isinstance(value, list):
        return [_move_tensors(torch, item, device) for item in value]
    if isinstance(value, dict):
        return {key: _move_tensors(torch, item, device) for key, item in value.items()}
    return value


def _controlnet_cpu_forward(controlnet, args, kwargs):
    torch = controlnet._torch_module
    output_device = kwargs["hidden_states"].device
    cpu_args = tuple(_move_tensors(torch, value, "cpu") for value in args)
    cpu_kwargs = {key: _move_tensors(torch, value, "cpu") for key, value in kwargs.items()}
    output = controlnet._original_forward(*cpu_args, **cpu_kwargs)
    return _move_tensors(torch, output, output_device)


def _enable_transformer_residual_placement(transformer):
    def block_residual(index, output):
        samples = transformer._controlnet_active_residuals
        if samples is None:
            return output
        interval = (len(transformer.transformer_blocks) + len(samples) - 1) // len(samples)
        encoder, hidden = output
        residual = samples[index // interval].to(hidden.device)
        return encoder, hidden + residual

    for index, block in enumerate(transformer.transformer_blocks):
        block.register_forward_hook(lambda _module, _args, output, index=index: block_residual(index, output))

    original_forward = transformer.forward

    def forward(*args, **kwargs):
        samples = kwargs.pop("controlnet_block_samples", None)
        if samples is None:
            return original_forward(*args, **kwargs)
        transformer._controlnet_active_residuals = samples
        try:
            return original_forward(*args, controlnet_block_samples=None, **kwargs)
        finally:
            transformer._controlnet_active_residuals = None

    transformer._controlnet_active_residuals = None
    transformer.forward = forward


class ImageRuntime:
    def __init__(self, snapshot: str, model: str, *, steps: int, threads: int = 8,
                 quantization: str = "nf4", unit: str | None = None,
                 depth_guide: dict | None = None, true_cfg_scale: float = 4.0,
                 negative_prompt: str = "", max_sequence_length: int = 512,
                 offload: dict | None = None):
        if depth_guide is not None:
            if not isinstance(depth_guide, dict):
                raise DepthConfigurationError("depth_guide must be a configuration object")
            validate_depth_variant(model, unit, quantization)
            validate_controlnet_quantization(depth_guide)
        import torch
        from diffusers import (BitsAndBytesConfig, QwenImagePipeline,
                               QwenImageTransformer2DModel, ZImagePipeline,
                               ZImageTransformer2DModel)
        torch.set_num_threads(threads)
        self.torch, self.model, self.steps = torch, model, steps
        self.quantization = quantization
        self.depth_guide = depth_guide
        self.depth_pipe = None
        self.controlnet = None
        self.true_cfg_scale = true_cfg_scale
        self.negative_prompt = negative_prompt
        self.max_sequence_length = max_sequence_length
        self.offload = validate_offload(offload)
        qwen = model == QWEN_IMAGE_MODEL
        self.qwen = qwen
        # Z-Image's BF16-trained layers overflow FP16 on Turing and emit black
        # PNGs. Torch/cu124 supports BF16 arithmetic on this qualified 2070;
        # the lack of native BF16 tensor cores affects speed, not this dtype.
        dtype = torch.bfloat16
        transformer_class = QwenImageTransformer2DModel if qwen else ZImageTransformer2DModel
        if quantization == "int8":
            quant_options = {"load_in_8bit": True}
            if self.offload is not None:
                quant_options["llm_int8_enable_fp32_cpu_offload"] = True
            quant = BitsAndBytesConfig(**quant_options)
        else:
            quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                       bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
        if self.offload is not None and self.offload["strategy"] == "auto":
            self._offload_tmp = tempfile.TemporaryDirectory(prefix="qwen-image-offload-")
            transformer_options = {"device_map": "auto",
                                   "max_memory": transformer_memory(self.offload),
                                   "offload_folder": self._offload_tmp.name}
        else:
            transformer_options = {"device_map": {"": "cpu" if self.offload is not None else "cuda:0"}}
        transformer = transformer_class.from_pretrained(
            snapshot, subfolder="transformer", torch_dtype=dtype,
            quantization_config=quant, local_files_only=True, **transformer_options)
        pipeline_class = QwenImagePipeline if qwen else ZImagePipeline
        text_pipe = pipeline_class.from_pretrained(
            snapshot, transformer=transformer, torch_dtype=dtype, local_files_only=True)
        if self.offload is not None and self.offload["strategy"] == "sequential":
            text_pipe.reset_device_map()
            text_pipe._exclude_from_cpu_offload = ["text_encoder"]
            if self.offload.get("vae") != "cpu":
                text_pipe._exclude_from_cpu_offload.insert(0, "vae")
            text_pipe.enable_sequential_cpu_offload()
        self.pipe = text_pipe
        # Text encoding never competes with the GPU-resident generator.
        text_pipe.text_encoder.to(device="cpu", dtype=torch.float32)
        if self.offload is None or self.offload.get("vae") != "cpu":
            text_pipe.vae.to(device="cuda:0", dtype=torch.bfloat16 if qwen else torch.float32)
        text_pipe.vae.enable_tiling()
        text_pipe.vae.enable_slicing()

        if depth_guide is not None:
            from diffusers import QwenImageControlNetModel, QwenImageControlNetPipeline
            controlnet_quantization = validate_controlnet_quantization(depth_guide)
            controlnet_options = {}
            controlnet_mode = self.offload.get("controlnet") if self.offload is not None else None
            controlnet_device = f"cuda:{self.offload.get('controlnet_device', 0)}" if self.offload is not None else "cuda:0"
            cpu_controlnet = controlnet_mode in ("sequential", "cpu")
            if controlnet_quantization == "int8":
                control_quant_options = {"load_in_8bit": True}
                if self.offload is not None:
                    control_quant_options["llm_int8_enable_fp32_cpu_offload"] = True
                controlnet_options = {
                    "quantization_config": BitsAndBytesConfig(**control_quant_options),
                    "device_map": {"": "cpu" if cpu_controlnet else controlnet_device},
                }
            controlnet = QwenImageControlNetModel.from_pretrained(
                depth_guide["snapshot"], torch_dtype=torch.bfloat16,
                local_files_only=True, **controlnet_options)
            if controlnet_mode == "sequential":
                if controlnet_quantization == "bf16":
                    controlnet.to(device="cpu", dtype=torch.bfloat16)
                from accelerate import cpu_offload
                cpu_offload(controlnet, torch.device(controlnet_device),
                            offload_buffers=len(controlnet._parameters) > 0)
            elif controlnet_mode == "cpu":
                if controlnet_quantization == "bf16":
                    controlnet.to(device="cpu", dtype=torch.bfloat16)
                controlnet._torch_module = torch
                controlnet._original_forward = controlnet.forward
                controlnet.forward = lambda *args, **kwargs: _controlnet_cpu_forward(controlnet, args, kwargs)
            elif controlnet_quantization == "bf16":
                controlnet.to(device=controlnet_device, dtype=torch.bfloat16)
            self.controlnet = controlnet
            self.depth_pipe = QwenImageControlNetPipeline(
                scheduler=text_pipe.scheduler,
                vae=text_pipe.vae,
                text_encoder=text_pipe.text_encoder,
                tokenizer=text_pipe.tokenizer,
                transformer=text_pipe.transformer,
                controlnet=controlnet,
            )
        self.offload_map = None
        if self.offload is not None:
            transformer_map = getattr(transformer, "hf_device_map", {})
            transformer_devices = ({value for value in transformer_map.values() if isinstance(value, int)}
                                   if isinstance(transformer_map, dict) else set())
            if (depth_guide is not None and self.offload["strategy"] == "auto"
                    and transformer_devices
                    and (len(transformer_devices) > 1
                         or self.offload.get("controlnet_device", 0) not in transformer_devices)):
                _enable_transformer_residual_placement(transformer)
            controlnet_map = getattr(self.controlnet, "hf_device_map", {}) if self.controlnet is not None else None
            if controlnet_mode in ("sequential", "cpu"):
                controlnet_map = f"{controlnet_mode}:{controlnet_device}"
            self.offload_map = {
                "transformer": transformer_map if isinstance(transformer_map, dict) else {},
                "controlnet": controlnet_map if isinstance(controlnet_map, (dict, str)) else None,
            }
            print(json.dumps({"event": "image_offload_map", **self.offload_map}, sort_keys=True), flush=True)

    def generate(self, request: dict) -> tuple[object, dict]:
        guided = "control_image" in request
        if guided and self.depth_guide is None:
            raise RuntimeError("depth guidance is not configured for this runtime")
        torch = self.torch
        pipe = self.depth_pipe if guided else self.pipe
        guide = None
        image_sha256 = None
        if guided:
            guide, image_sha256 = _decode_control_image(
                request["control_image"], request["width"], request["height"])
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            if self.qwen:
                embeds, mask = pipe.encode_prompt(
                    request["prompt"], device=torch.device("cpu"),
                    max_sequence_length=self.max_sequence_length)
                neg, neg_mask = pipe.encode_prompt(
                    self.negative_prompt, device=torch.device("cpu"),
                    max_sequence_length=self.max_sequence_length)
                kwargs = dict(prompt_embeds=embeds.to("cuda:0", dtype=pipe.transformer.dtype),
                              prompt_embeds_mask=mask.to("cuda:0") if mask is not None else None,
                              negative_prompt_embeds=neg.to("cuda:0", dtype=pipe.transformer.dtype),
                              negative_prompt_embeds_mask=neg_mask.to("cuda:0") if neg_mask is not None else None,
                              true_cfg_scale=self.true_cfg_scale)
            else:
                embeds, _ = pipe.encode_prompt(request["prompt"], device=torch.device("cpu"), do_classifier_free_guidance=False)
                kwargs = dict(prompt_embeds=[e.to("cuda:0", dtype=pipe.transformer.dtype) for e in embeds], guidance_scale=0.0)
            encoded = time.monotonic()
            free_cuda()
            call_kwargs = dict(width=request["width"], height=request["height"],
                               num_inference_steps=self.steps,
                               generator=torch.Generator("cuda:0").manual_seed(request["seed"]))
            if guided:
                call_kwargs.update(
                    control_image=guide,
                    controlnet_conditioning_scale=request["controlnet_conditioning_scale"],
                    control_guidance_start=0.0,
                    control_guidance_end=1.0,
                )
            image = pipe(**kwargs, **call_kwargs).images[0]
        if guided and image.size != (request["width"], request["height"]):
            raise RuntimeError(
                f"guided image dimensions {image.size} do not match the requested output dimensions")
        validate_image(image)
        depth_metrics = None
        if guided:
            depth_metrics = {
                "image_sha256": image_sha256,
                "conditioning_scale": request["controlnet_conditioning_scale"],
                "controlnet_model": self.depth_guide["model"],
                "controlnet_revision": self.depth_guide["revision"],
                "controlnet_config_sha256": self.depth_guide["config_sha256"],
                "controlnet_quantization": self.depth_guide.get("quantization", "bf16"),
                "control_guidance_start": 0.0,
                "control_guidance_end": 1.0,
            }
        metrics = {"steps": self.steps, "quantization": self.quantization,
                   "offload": self.offload, "offload_map": self.offload_map,
                   "text_encoder": "CPU FP32", "compute_dtype": str(pipe.transformer.dtype),
                   "vae_dtype": str(pipe.vae.dtype), "depth_guide": depth_metrics,
                   "prompt_encoding_s": round(encoded-started, 3),
                   "generation_s": round(time.monotonic()-encoded, 3),
                   "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                   "peak_reserved_bytes": torch.cuda.max_memory_reserved()}
        if self.offload is not None:
            devices = sorted(transformer_memory(self.offload).keys() - {"cpu"}) if self.offload["strategy"] == "auto" else [0]
            controlnet_device = self.offload.get("controlnet_device", 0)
            devices = sorted(set(devices) | {controlnet_device})
            metrics["device_peaks"] = {f"cuda:{device}": {
                "allocated_bytes": torch.cuda.max_memory_allocated(device),
                "reserved_bytes": torch.cuda.max_memory_reserved(device),
            } for device in devices}
        if self.depth_guide is not None:
            metrics["generation_settings"] = {
                "steps": self.steps,
                "true_cfg_scale": self.true_cfg_scale,
                "negative_prompt": self.negative_prompt,
                "text_encoder": "CPU FP32",
                "max_sequence_length": self.max_sequence_length,
            }
        return image, metrics

    def close(self):
        self.pipe = self.depth_pipe = self.controlnet = None
        gc.collect()
        self.torch.cuda.empty_cache()
        temporary = getattr(self, "_offload_tmp", None)
        if temporary is not None:
            temporary.cleanup()
            self._offload_tmp = None
