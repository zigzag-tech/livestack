"""Diffusers runtime: quantized generator on GPU, text encoder on CPU."""
from __future__ import annotations

import gc
import time


def validate_image(image):
    from PIL import ImageStat
    if max(ImageStat.Stat(image.convert("RGB")).stddev) < 2:
        raise RuntimeError("image output is nearly blank; refusing a false inference success")


class ImageRuntime:
    def __init__(self, snapshot: str, model: str, *, steps: int, threads: int = 8, quantization: str = "nf4"):
        import torch
        from diffusers import (BitsAndBytesConfig, QwenImagePipeline,
                               QwenImageTransformer2DModel, ZImagePipeline,
                               ZImageTransformer2DModel)
        torch.set_num_threads(threads)
        self.torch, self.model, self.steps = torch, model, steps
        self.quantization = quantization
        qwen = model == "Qwen/Qwen-Image-2512"
        self.qwen = qwen
        # Z-Image's BF16-trained layers overflow FP16 on Turing and emit black
        # PNGs. Torch/cu124 supports BF16 arithmetic on this qualified 2070;
        # the lack of native BF16 tensor cores affects speed, not this dtype.
        dtype = torch.bfloat16
        transformer_class = QwenImageTransformer2DModel if qwen else ZImageTransformer2DModel
        quant = (BitsAndBytesConfig(load_in_8bit=True) if quantization == "int8" else
                 BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                    bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype))
        transformer = transformer_class.from_pretrained(
            snapshot, subfolder="transformer", torch_dtype=dtype,
            quantization_config=quant, device_map={"": "cuda:0"}, local_files_only=True)
        pipeline_class = QwenImagePipeline if qwen else ZImagePipeline
        self.pipe = pipeline_class.from_pretrained(
            snapshot, transformer=transformer, torch_dtype=dtype, local_files_only=True)
        # Text encoding never competes with the GPU-resident generator.
        self.pipe.text_encoder.to(device="cpu", dtype=torch.float32)
        self.pipe.vae.to(device="cuda:0", dtype=torch.bfloat16 if qwen else torch.float32)
        self.pipe.vae.enable_tiling()
        self.pipe.vae.enable_slicing()

    def generate(self, request: dict) -> tuple[object, dict]:
        torch, pipe = self.torch, self.pipe
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            if self.qwen:
                embeds, mask = pipe.encode_prompt(request["prompt"], device=torch.device("cpu"), max_sequence_length=512)
                neg, neg_mask = pipe.encode_prompt("", device=torch.device("cpu"), max_sequence_length=512)
                kwargs = dict(prompt_embeds=embeds.to("cuda:0", dtype=pipe.transformer.dtype),
                              prompt_embeds_mask=mask.to("cuda:0") if mask is not None else None,
                              negative_prompt_embeds=neg.to("cuda:0", dtype=pipe.transformer.dtype),
                              negative_prompt_embeds_mask=neg_mask.to("cuda:0") if neg_mask is not None else None,
                              true_cfg_scale=4.0)
            else:
                embeds, _ = pipe.encode_prompt(request["prompt"], device=torch.device("cpu"), do_classifier_free_guidance=False)
                kwargs = dict(prompt_embeds=[e.to("cuda:0", dtype=pipe.transformer.dtype) for e in embeds], guidance_scale=0.0)
            encoded = time.monotonic()
            image = pipe(**kwargs, width=request["width"], height=request["height"],
                         num_inference_steps=self.steps,
                         generator=torch.Generator("cuda:0").manual_seed(request["seed"])).images[0]
        validate_image(image)
        return image, {"steps": self.steps, "quantization": self.quantization,
                       "text_encoder": "CPU FP32", "compute_dtype": str(pipe.transformer.dtype),
                       "vae_dtype": str(pipe.vae.dtype),
                       "prompt_encoding_s": round(encoded-started, 3),
                       "generation_s": round(time.monotonic()-encoded, 3),
                       "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                       "peak_reserved_bytes": torch.cuda.max_memory_reserved()}

    def close(self):
        self.pipe = None
        gc.collect()
        self.torch.cuda.empty_cache()
