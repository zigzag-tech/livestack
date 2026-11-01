"""FLUX.2 Fun Union depth runtime with INT8 transformer and configured offload."""
from __future__ import annotations

import gc
import json
import tempfile
import time
from pathlib import Path

from ..freeing import free_cuda
from .runtime import (DepthConfigurationError, FLUX2_IMAGE_MODEL,
                      _decode_control_image, validate_depth_variant,
                      validate_image)


class Flux2ImageRuntime:
    def __init__(self, snapshot: str, model: str, *, steps: int, threads: int = 8,
                 quantization: str = "int8", quantization_backend: str = "bitsandbytes",
                 unit: str | None = None,
                 depth_guide: dict | None = None, guidance_scale: float = 4.0,
                 negative_prompt: str = "", offload: dict | None = None,
                 videox_fun_root: str | None = None, **_unused):
        if model != FLUX2_IMAGE_MODEL or not isinstance(depth_guide, dict):
            raise DepthConfigurationError("FLUX.2 requires its base model and depth_guide configuration")
        validate_depth_variant(model, unit, quantization)
        if not videox_fun_root:
            raise DepthConfigurationError("FLUX.2 requires a pinned videox_fun_root")
        if not isinstance(offload, dict) or offload.get("strategy") != "auto":
            raise DepthConfigurationError("FLUX.2 requires automatic device offload")
        import sys
        sys.path.insert(0, videox_fun_root)
        import torch
        from accelerate import dispatch_model, infer_auto_device_map
        from accelerate.utils import send_to_device
        from diffusers import FlowMatchEulerDiscreteScheduler
        from safetensors.torch import load_file
        from videox_fun.models import (AutoencoderKLFlux2, Flux2ControlTransformer2DModel,
                                       Mistral3ForConditionalGeneration, PixtralProcessor)
        from videox_fun.pipeline import Flux2ControlPipeline
        if quantization_backend == "bitsandbytes":
            from diffusers import BitsAndBytesConfig
            from transformers import BitsAndBytesConfig as TransformersBitsAndBytesConfig
            transformer_quantization = BitsAndBytesConfig(
                load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True)
            text_quantization = TransformersBitsAndBytesConfig(
                load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=True)
        elif quantization_backend == "torchao":
            from diffusers import TorchAoConfig
            from torchao.quantization import Int8WeightOnlyConfig
            from transformers import TorchAoConfig as TransformersTorchAoConfig
            transformer_quantization = TorchAoConfig(Int8WeightOnlyConfig())
            text_quantization = TransformersTorchAoConfig(Int8WeightOnlyConfig())
        else:
            raise DepthConfigurationError("FLUX.2 quantization_backend must be bitsandbytes or torchao")
        torch.set_num_threads(threads)
        self.torch, self.model, self.steps = torch, model, steps
        self.depth_guide = depth_guide
        self.guidance_scale = guidance_scale
        self.negative_prompt = negative_prompt
        self.quantization = quantization
        self.quantization_backend = quantization_backend
        self.device = int(offload.get("controlnet_device", 0))
        transformer = Flux2ControlTransformer2DModel.from_pretrained(
            snapshot, subfolder="transformer", low_cpu_mem_usage=True,
            torch_dtype=torch.bfloat16,
            transformer_additional_kwargs={"control_layers": [0, 2, 4, 6], "control_in_dim": 260},
            quantization_config=transformer_quantization)
        control_state = load_file(str(Path(depth_guide["snapshot"]) / "diffusion_pytorch_model.safetensors"))
        missing, unexpected = transformer.load_state_dict(control_state, strict=False)
        if unexpected:
            raise DepthConfigurationError(f"FLUX.2 controller has unexpected keys: {unexpected[:8]}")
        missing_control = [key for key in missing if key.startswith("control_")
                           or ".before_proj" in key or ".after_proj" in key]
        if missing_control:
            raise DepthConfigurationError(f"FLUX.2 controller keys are missing: {missing_control[:8]}")
        meta = [name for name, value in transformer.named_parameters() if value.is_meta]
        meta += [name for name, value in transformer.named_buffers() if value.is_meta]
        if meta:
            raise DepthConfigurationError(f"FLUX.2 tensors were not materialized: {meta[:8]}")
        budgets = offload.get("transformer_gib")
        max_memory = {int(device): f"{value}GiB" for device, value in budgets.items()}
        max_memory["cpu"] = f"{offload.get('cpu_gib', 48)}GiB"
        device_map = infer_auto_device_map(
            transformer, max_memory=max_memory,
            no_split_module_classes=[
                "Flux2SingleTransformerBlock", "Flux2TransformerBlock",
                "Flux2ControlTransformerBlock",
            ])
        for prefix in ("transformer_blocks", "control_transformer_blocks"):
            if prefix in device_map:
                device = device_map.pop(prefix)
                for name, _child in transformer.get_submodule(prefix).named_children():
                    device_map[f"{prefix}.{name}"] = device
        self.gpu_devices = sorted({device for device in device_map.values()
                                   if isinstance(device, int)})
        if self.device not in device_map.values():
            raise DepthConfigurationError("FLUX.2 INT8 transformer has no GPU-resident blocks")
        self._offload_tmp = tempfile.TemporaryDirectory(
            prefix="flux2-offload-", dir="/var/lib/livestack-workloads/xc-tower-e2e")
        dispatch_model(transformer, device_map, offload_dir=self._offload_tmp.name)
        workspaces = [device for device in self.gpu_devices if device != self.device]
        if workspaces:
            def align_on(device):
                def align_workspace(_module, args, kwargs):
                    return send_to_device(args, device), send_to_device(kwargs, device)
                return align_workspace
            double_blocks = [(name, module) for name, module in transformer.named_modules()
                             if name.startswith("transformer_blocks.") and name.count(".") == 1]
            for name, module in double_blocks:
                devices = {device for key, device in device_map.items()
                           if (key == name or key.startswith(name + ".")) and isinstance(device, int)}
                workspace = devices.pop() if len(devices) == 1 else self.device
                for _child_name, child in module.named_modules():
                    if hasattr(child, "_hf_hook"):
                        child._hf_hook.execution_device = workspace
                module.register_forward_pre_hook(align_on(workspace), with_kwargs=True)
            cpu_blocks = [(name, module) for name, module in transformer.named_modules()
                          if name and device_map.get(name) == "cpu" and hasattr(module, "_hf_hook")
                          and not name.startswith("transformer_blocks.")]
            for index, (name, module) in enumerate(cpu_blocks):
                workspace = (self.device if name.startswith((
                                 "control_transformer_blocks.", "control_img_in"))
                             or name in {"norm_out", "proj_out"}
                             else self.gpu_devices[index % len(self.gpu_devices)])
                for _child_name, child in module.named_modules():
                    if hasattr(child, "_hf_hook"):
                        child._hf_hook.execution_device = workspace
                module.register_forward_pre_hook(align_on(workspace), with_kwargs=True)
            def return_to_primary(_module, _inputs, output):
                if isinstance(output, tuple):
                    return (output[0].to(self.device), *output[1:])
                output.sample = output.sample.to(self.device)
                return output
            transformer.register_forward_hook(return_to_primary)
        if offload.get("trace_memory"):
            def trace(name):
                def log(*_args):
                    print(json.dumps({"event": "flux2_memory", "module": name,
                                      "allocated": {device: torch.cuda.memory_allocated(device)
                                                    for device in self.gpu_devices}}), flush=True)
                    handle.remove()
                handle = transformer.get_submodule(name).register_forward_hook(log)
            for name in device_map:
                if name.startswith(("single_transformer_blocks.", "transformer_blocks.",
                                    "control_transformer_blocks.")) and name.count(".") == 1:
                    trace(name)
        self.offload_map = {"transformer": device_map, "controlnet": "integrated"}
        print(json.dumps({"event": "image_offload_map", **self.offload_map}, default=str), flush=True)
        vae = AutoencoderKLFlux2.from_pretrained(snapshot, subfolder="vae").to(torch.bfloat16).to(self.device)
        tokenizer = PixtralProcessor.from_pretrained(snapshot, subfolder="tokenizer")
        text_encoder = Mistral3ForConditionalGeneration.from_pretrained(
            snapshot, subfolder="text_encoder", torch_dtype=torch.bfloat16,
            quantization_config=text_quantization,
            device_map={"": "cpu"}, low_cpu_mem_usage=True)
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(snapshot, subfolder="scheduler")
        self.pipe = Flux2ControlPipeline(
            vae=vae, tokenizer=tokenizer, text_encoder=text_encoder,
            transformer=transformer, scheduler=scheduler)
        self.offload = offload

    def generate(self, request: dict):
        torch = self.torch
        guide, image_sha256 = _decode_control_image(
            request["control_image"], request["width"], request["height"])
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            embeds, _ = self.pipe.encode_prompt(
                request["prompt"], device=torch.device("cpu"))
            encoded = time.monotonic()
            free_cuda()
            image = self.pipe(
                prompt=None, prompt_embeds=embeds.to(self.device, dtype=torch.bfloat16),
                control_image=guide,
                control_context_scale=request["controlnet_conditioning_scale"],
                width=request["width"], height=request["height"],
                num_inference_steps=self.steps, guidance_scale=self.guidance_scale,
                generator=torch.Generator(self.device).manual_seed(request["seed"])).images[0]
        if image.size != (request["width"], request["height"]):
            raise RuntimeError("guided image dimensions do not match the request")
        validate_image(image)
        depth_metrics = {
            "image_sha256": image_sha256,
            "conditioning_scale": request["controlnet_conditioning_scale"],
            "controlnet_model": self.depth_guide["model"],
            "controlnet_revision": self.depth_guide["revision"],
            "controlnet_config_sha256": self.depth_guide["config_sha256"],
            "controlnet_quantization": "int8",
            "control_guidance_start": 0.0,
            "control_guidance_end": 1.0,
        }
        return image, {
            "steps": self.steps, "quantization": self.quantization,
            "quantization_backend": self.quantization_backend,
            "offload": self.offload, "offload_map": self.offload_map,
            "text_encoder": "CPU INT8", "compute_dtype": "torch.bfloat16",
            "vae_dtype": str(self.pipe.vae.dtype), "depth_guide": depth_metrics,
            "prompt_encoding_s": round(encoded-started, 3),
            "generation_s": round(time.monotonic()-encoded, 3),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            "device_peaks": {f"cuda:{device}": {
                "allocated_bytes": torch.cuda.max_memory_allocated(device),
                "reserved_bytes": torch.cuda.max_memory_reserved(device),
            } for device in self.gpu_devices},
            "generation_settings": {
                "steps": self.steps, "guidance_scale": self.guidance_scale,
                "negative_prompt": self.negative_prompt, "text_encoder": "CPU INT8",
                "quantization_backend": self.quantization_backend,
            },
        }

    def close(self):
        self.pipe = None
        gc.collect()
        self.torch.cuda.empty_cache()
        temporary = getattr(self, "_offload_tmp", None)
        if temporary is not None:
            temporary.cleanup()
            self._offload_tmp = None
