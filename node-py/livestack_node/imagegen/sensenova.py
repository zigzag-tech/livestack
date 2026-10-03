"""SenseNova-U1.5 GGUF runtime, served by the shared Harmony image worker.

The complete MoT has separate understanding and generation weights. Layer
streaming keeps the quantized weights in host RAM; admission must therefore
charge both ram_bytes and vram_bytes in the worker's resident_bytes vector.
"""
from __future__ import annotations

import gc
import json
import os
import time
from functools import partial
from pathlib import Path

MODEL = "sensenova/SenseNova-U1.5-8B-MoT"


def restore_bf16_modules(model, checkpoint):
    """Restore explicitly supplied sensitive modules from official weights."""
    import torch
    from safetensors.torch import load_file
    from diffusers.quantizers.gguf.utils import GGUFLinear
    weights = load_file(checkpoint)
    for name, weight in weights.items():
        if not name.startswith("fm_modules.") or "layers." in name:
            raise ValueError("BF16 overrides must contain only SenseNova FM modules")
        module_name, parameter = name.rsplit(".", 1)
        module = model.get_submodule(module_name)
        if isinstance(module, GGUFLinear):
            parent_name, child = module_name.rsplit(".", 1)
            dense = torch.nn.Linear(module.in_features, module.out_features,
                                    bias=module.bias is not None, device="meta")
            dense.weight, dense.bias = module.weight, module.bias
            setattr(model.get_submodule(parent_name), child, dense)
            module = dense
        setattr(module, parameter, torch.nn.Parameter(weight.to(torch.bfloat16), requires_grad=False))


def restore_timestep_dtype(model):
    """Keep the upstream timestep dtype anchor floating, even in packed GGUFs.

    TimestepEmbedder.forward casts its sinusoidal input to mlp[0].weight.dtype.
    GGUF stores that weight as uint8; restoring this small linear to the same
    dequantized BF16 values prevents byte inputs without changing the model.
    """
    import torch
    from diffusers.quantizers.gguf.utils import GGUFLinear, dequantize_gguf_tensor
    from sensenova_u1.models.neo_unify.modeling_fm_modules import TimestepEmbedder
    for embedder in model.fm_modules.values():
        if not isinstance(embedder, TimestepEmbedder):
            continue
        mlp = embedder.mlp
        packed = mlp[0]
        if isinstance(packed, GGUFLinear):
            weight = dequantize_gguf_tensor(packed.weight).to(packed.compute_dtype)
            dense = torch.nn.Linear(packed.in_features, packed.out_features,
                                    bias=packed.bias is not None, device="meta")
            dense.weight = torch.nn.Parameter(weight, requires_grad=False)
            dense.bias = packed.bias
            mlp[0] = dense


class SenseNovaRuntime:
    def __init__(self, snapshot, model, *, steps, threads=8, quantization="Q3_K_M", config):
        if model != MODEL or quantization not in ("Q2_K", "Q3_K_M", "Q5_K_M", "Q6_K", "Q8_0"):
            raise ValueError("SenseNova runtime requires U1.5 MoT and a supported GGUF quantization")
        import torch
        import sensenova_u1  # Registers the official architecture with Transformers.
        from sensenova_u1.utils import load_model_and_tokenizer
        torch.set_num_threads(threads)
        self.torch, self.steps = torch, steps
        self.quantization = quantization
        self.mode = config.get("vram_mode", "low")
        if self.mode not in ("low", "balanced", "fast", "full"):
            raise ValueError("invalid vram_mode")
        self.model, self.tokenizer = load_model_and_tokenizer(
            snapshot, dtype=torch.bfloat16, device="cuda:0",
            gguf_checkpoint=config["gguf_checkpoint"], for_offload=self.mode != "full")
        restore_timestep_dtype(self.model)
        self.bf16_modules = bool(config.get("bf16_modules_checkpoint"))
        if config.get("bf16_modules_checkpoint"):
            restore_bf16_modules(self.model, config["bf16_modules_checkpoint"])
        self.cfg_scale = config.get("cfg_scale", 4.0)

    def generate(self, request):
        from PIL import Image
        from sensenova_u1.utils import make_offload_ctx, vram_mode_to_prefetch_count
        from .runtime import validate_image
        torch = self.torch
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode(), make_offload_ctx(
            self.model, vram_mode_to_prefetch_count(self.mode), "cuda:0",
            keep_generation_resident=self.mode == "fast",
        ) as model:
            pixels = model.t2i_generate(
                self.tokenizer, request["prompt"],
                image_size=(request["width"], request["height"]),
                cfg_scale=self.cfg_scale, cfg_norm="none", timestep_shift=3.0,
                num_steps=self.steps, batch_size=1, seed=request["seed"], think_mode=False)
            pixels = ((pixels[0].float() * 0.5 + 0.5).clamp(0, 1)
                      .permute(1, 2, 0).cpu().numpy())
            image = Image.fromarray((pixels * 255).round().astype("uint8"))
        torch.cuda.synchronize()
        validate_image(image)
        return image, {"steps": self.steps, "quantization": self.quantization,
                       "official_bf16_fm_modules": self.bf16_modules,
                       "vram_mode": self.mode, "cfg_scale": self.cfg_scale,
                       "compute_dtype": "torch.bfloat16",
                       "generation_s": round(time.monotonic() - started, 3),
                       "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                       "peak_reserved_bytes": torch.cuda.max_memory_reserved()}

    def close(self):
        from livestack_node.freeing import free_cuda, trim_ram
        self.model = self.tokenizer = None
        gc.collect()
        free_cuda()
        trim_ram()


def main():
    import uvicorn
    from .worker import create_app
    config = json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    uvicorn.run(create_app(config, runtime_factory=partial(SenseNovaRuntime, config=config)),
                host="0.0.0.0", port=config["port"])


if __name__ == "__main__":
    main()
