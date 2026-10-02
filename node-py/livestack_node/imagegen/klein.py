"""FLUX.2-klein-4B runtime for the Harmony image worker (`imagegen.worker`).

Deployed on zz-joe as `harmony-klein-{0,1}` (one process per RTX 2070):

    HARMONY_IMAGE_CONFIG=~/harmony-image/klein/worker-N.json \\
        python -m livestack_node.imagegen.klein

Everything the unit holds lives on its GPU for the whole residency: the NF4
transformer, the NF4 text encoder and the VAE. Host RAM never holds weights.
The worker config's `resident_bytes` is only the declared PRIOR for the
planner; the node measures what a load and a generation actually take
(`measure.ActivationObserver`) and reports that instead once it has it.
"""
import gc
import time

MODEL = "black-forest-labs/FLUX.2-klein-4B"
VAE_TILE_PIXELS = 512


def configure_vae_tiling(vae):
    """Tile VAE decoding at 512 px. diffusers' default threshold is 1024 px, so a
    768 px image decoded in one piece and OOMed an 8 GB card (zz-joe 2026-10-02)."""
    vae.enable_tiling()
    vae.enable_slicing()
    vae.tile_sample_min_size = VAE_TILE_PIXELS
    vae.tile_latent_min_size = VAE_TILE_PIXELS // (2 ** (len(vae.config.block_out_channels) - 1))


class KleinRuntime:
    def __init__(self, snapshot, model, *, steps, threads=8, quantization="none"):
        if model != MODEL or steps != 4 or quantization not in ("none", "nf4"):
            raise ValueError("this runtime requires distilled Klein 4B, four steps, and none/NF4 quantization")
        import torch
        from diffusers import BitsAndBytesConfig, Flux2KleinPipeline, Flux2Transformer2DModel
        from transformers import AutoModelForCausalLM, BitsAndBytesConfig as TextQuantization
        torch.set_num_threads(threads)
        self.torch, self.steps = torch, steps
        self.quantization = quantization
        kwargs = {}
        if quantization == "nf4":
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)
        # The selected worker's visible device is its broker-advertised device.
        # This runtime never chooses another GPU or bypasses Harmony admission.
        transformer = Flux2Transformer2DModel.from_pretrained(
            snapshot, subfolder="transformer", torch_dtype=torch.bfloat16,
            device_map={"": "cuda:0"}, local_files_only=True, **kwargs)
        text_encoder = AutoModelForCausalLM.from_pretrained(
            snapshot, subfolder="text_encoder", torch_dtype=torch.bfloat16,
            quantization_config=TextQuantization(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16),
            device_map={"": "cuda:0"}, local_files_only=True)
        self.pipe = Flux2KleinPipeline.from_pretrained(
            snapshot, transformer=transformer, text_encoder=text_encoder, torch_dtype=torch.bfloat16,
            local_files_only=True)
        self.pipe.vae.to(device="cuda:0", dtype=torch.bfloat16)
        configure_vae_tiling(self.pipe.vae)
        # The NF4 text encoder stays on this GPU for the unit's whole residency.
        # It used to be parked in host RAM between prompts, which put model
        # weights in the host's memory budget (zz-joe 2026-10-02: host RAM, not
        # VRAM, was what e2e work was refused for). Host RAM never holds them.
        torch.cuda.empty_cache()

    def generate(self, request):
        from livestack_node.imagegen.runtime import validate_image
        torch, pipe = self.torch, self.pipe
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            embeds, _ = pipe.encode_prompt(request["prompt"], device=torch.device("cuda:0"))
            if not torch.isfinite(embeds).all():
                raise RuntimeError("non-finite text embeddings")
            encoded = time.monotonic()
            image = pipe(prompt_embeds=embeds.to("cuda:0", dtype=pipe.transformer.dtype),
                         width=request["width"], height=request["height"],
                         num_inference_steps=4, guidance_scale=1.0,
                         generator=torch.Generator("cuda:0").manual_seed(request["seed"])).images[0]
        torch.cuda.synchronize()
        validate_image(image)
        del embeds
        torch.cuda.empty_cache()
        return image, {"steps": 4, "quantization": self.quantization, "text_encoder": "GPU NF4; resident",
                       "vae_tile_pixels": VAE_TILE_PIXELS,
                       "compute_dtype": str(pipe.transformer.dtype), "vae_dtype": str(pipe.vae.dtype),
                       "prompt_encoding_s": round(encoded-started, 3),
                       "generation_s": round(time.monotonic()-encoded, 3),
                       "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                       "peak_reserved_bytes": torch.cuda.max_memory_reserved()}

    def close(self):
        self.pipe = None
        gc.collect()
        self.torch.cuda.empty_cache()


def main():
    import json
    import os
    from pathlib import Path
    import uvicorn
    from livestack_node.imagegen.worker import create_app
    config = json.loads(Path(os.environ["HARMONY_IMAGE_CONFIG"]).read_text())
    uvicorn.run(create_app(config, runtime_factory=KleinRuntime), host="0.0.0.0", port=config["port"])


if __name__ == "__main__":
    main()
