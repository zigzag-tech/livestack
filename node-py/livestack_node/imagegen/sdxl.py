"""SDXL depth-guided image runtime with INT8 UNet and CPU text encoding."""
from __future__ import annotations

import gc
import time

from ..freeing import free_cuda
from .runtime import (DepthConfigurationError, SDXL_IMAGE_MODEL,
                      _decode_control_image, validate_controlnet_quantization,
                      validate_depth_variant, validate_image)


class SDXLImageRuntime:
    def __init__(self, snapshot: str, model: str, *, steps: int, threads: int = 8,
                 quantization: str = "int8", unit: str | None = None,
                 depth_guide: dict | None = None, guidance_scale: float = 7.5,
                 negative_prompt: str = "blurry, low quality, distorted geometry",
                 offload: dict | None = None, **_unused):
        if model != SDXL_IMAGE_MODEL or not isinstance(depth_guide, dict):
            raise DepthConfigurationError("SDXL requires the SDXL model and a depth_guide configuration")
        validate_depth_variant(model, unit, quantization)
        validate_controlnet_quantization(depth_guide)
        if offload is not None:
            raise DepthConfigurationError("SDXL depth does not support transformer offload settings")
        import torch
        from diffusers import (BitsAndBytesConfig, ControlNetModel,
                               StableDiffusionXLControlNetPipeline,
                               UNet2DConditionModel)
        torch.set_num_threads(threads)
        self.torch, self.model, self.steps = torch, model, steps
        self.depth_guide = depth_guide
        self.guidance_scale = guidance_scale
        self.negative_prompt = negative_prompt
        quant = BitsAndBytesConfig(load_in_8bit=True)
        unet = UNet2DConditionModel.from_pretrained(
            snapshot, subfolder="unet", torch_dtype=torch.bfloat16,
            quantization_config=quant, device_map={"": "cuda:0"}, local_files_only=True)
        controlnet = ControlNetModel.from_pretrained(
            depth_guide["snapshot"], torch_dtype=torch.bfloat16,
            local_files_only=True).to("cuda:0")
        unet_forward = unet.forward

        def aligned_unet_forward(*args, **kwargs):
            args = tuple(value.to(torch.bfloat16) if torch.is_tensor(value) else value
                         for value in args)
            kwargs = {key: value.to(torch.bfloat16) if torch.is_tensor(value) else value
                      for key, value in kwargs.items()}
            return unet_forward(*args, **kwargs)

        unet.forward = aligned_unet_forward
        controlnet_forward = controlnet.forward

        def aligned_controlnet_forward(*args, **kwargs):
            args = tuple(value.to(torch.bfloat16) if torch.is_tensor(value) else value
                         for value in args)
            kwargs = {key: value.to(torch.bfloat16) if torch.is_tensor(value) else value
                      for key, value in kwargs.items()}
            return controlnet_forward(*args, **kwargs)

        controlnet.forward = aligned_controlnet_forward
        self.pipe = StableDiffusionXLControlNetPipeline.from_pretrained(
            snapshot, unet=unet, controlnet=controlnet, torch_dtype=torch.bfloat16,
            local_files_only=True)
        self.pipe.text_encoder.to(device="cpu", dtype=torch.float32)
        self.pipe.text_encoder_2.to(device="cpu", dtype=torch.float32)
        self.pipe.vae.to(device="cuda:0", dtype=torch.float32)
        vae_decode = self.pipe.vae.decode

        def fp32_vae_decode(latents, *args, **kwargs):
            return vae_decode(latents.to(torch.float32), *args, **kwargs)

        self.pipe.vae.decode = fp32_vae_decode
        self.pipe.vae.enable_tiling()
        self.pipe.vae.enable_slicing()
        self.controlnet = controlnet
        self.quantization = quantization
        self.offload_map = None

    def generate(self, request: dict):
        torch = self.torch
        guide, image_sha256 = _decode_control_image(
            request["control_image"], request["width"], request["height"])
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            embeds, negative, pooled, negative_pooled = self.pipe.encode_prompt(
                request["prompt"], device=torch.device("cpu"),
                negative_prompt=self.negative_prompt, do_classifier_free_guidance=True)
            dtype = torch.bfloat16
            kwargs = dict(
                prompt_embeds=embeds.to("cuda:0", dtype=dtype),
                negative_prompt_embeds=negative.to("cuda:0", dtype=dtype),
                pooled_prompt_embeds=pooled.to("cuda:0", dtype=dtype),
                negative_pooled_prompt_embeds=negative_pooled.to("cuda:0", dtype=dtype),
                image=guide,
                controlnet_conditioning_scale=request["controlnet_conditioning_scale"],
                control_guidance_start=0.0,
                control_guidance_end=1.0,
                width=request["width"], height=request["height"],
                num_inference_steps=self.steps,
                guidance_scale=self.guidance_scale,
                generator=torch.Generator("cuda:0").manual_seed(request["seed"]))
            encoded = time.monotonic()
            free_cuda()
            image = self.pipe(**kwargs).images[0]
        if image.size != (request["width"], request["height"]):
            raise RuntimeError("guided image dimensions do not match the request")
        validate_image(image)
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
        return image, {
            "steps": self.steps, "quantization": self.quantization,
            "offload": None, "offload_map": None,
            "text_encoder": "CPU FP32", "compute_dtype": str(self.pipe.unet.dtype),
            "vae_dtype": str(self.pipe.vae.dtype), "depth_guide": depth_metrics,
            "prompt_encoding_s": round(encoded-started, 3),
            "generation_s": round(time.monotonic()-encoded, 3),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            "generation_settings": {
                "steps": self.steps, "guidance_scale": self.guidance_scale,
                "negative_prompt": self.negative_prompt, "text_encoder": "CPU FP32",
            },
        }

    def close(self):
        self.pipe = self.controlnet = None
        gc.collect()
        self.torch.cuda.empty_cache()
