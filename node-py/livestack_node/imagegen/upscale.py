"""Real-ESRGAN runtime: deterministic tiled upscaling, no dependence on diffusers.

Why a fidelity upscaler rather than a generative one: the fleet's conference and
document photos carry Chinese signage, banners and plaques, and a diffusion
restorer is free to invent glyphs. Real-ESRGAN x4plus only sharpens what is
there, runs in seconds on a 3090, and needs ~1 GB of VRAM beyond its weights.

The RRDB architecture is vendored here (BasicSR's `rrdbnet_arch.py`) so the
worker does not add `basicsr` (which pins old torch) or `spandrel` to the image
worker's environment. Weights are pinned by SHA-256 in the worker config and
verified before the unit advertises itself, like the ControlNet units do.
"""
from __future__ import annotations

import base64
import gc
import hashlib
import io
import time
from pathlib import Path

from livestack_node.freeing import free_cuda

from .runtime import GenerationCancelled

REALESRGAN_MODEL = "xinntao/Real-ESRGAN-x4plus"
#: Every model name this module can serve. The worker advertises `task=upscale`.
UPSCALE_MODELS = (REALESRGAN_MODEL,)

NETWORK_SCALE = 4          # the pinned checkpoint is the 4x network
TILE = 512                 # patch size; 3090 peaks well under 2 GiB at this size
TILE_PAD = 16              # context pixels around each patch, cropped from the output


class UpscaleConfigurationError(ValueError):
    pass


def _conv_block(torch, in_ch, out_ch):
    return torch.nn.Conv2d(in_ch, out_ch, 3, 1, 1)


def _build_rrdbnet(torch):
    """BasicSR RRDBNet(3, 3, 64, 23, gc=32, scale=4), the shape of RealESRGAN_x4plus.pth."""

    class ResidualDenseBlock(torch.nn.Module):
        def __init__(self, num_feat=64, num_grow_ch=32):
            super().__init__()
            self.conv1 = _conv_block(torch, num_feat, num_grow_ch)
            self.conv2 = _conv_block(torch, num_feat + num_grow_ch, num_grow_ch)
            self.conv3 = _conv_block(torch, num_feat + 2 * num_grow_ch, num_grow_ch)
            self.conv4 = _conv_block(torch, num_feat + 3 * num_grow_ch, num_grow_ch)
            self.conv5 = _conv_block(torch, num_feat + 4 * num_grow_ch, num_feat)
            self.lrelu = torch.nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(torch.nn.Module):
        def __init__(self, num_feat, num_grow_ch=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

        def forward(self, x):
            out = self.rdb3(self.rdb2(self.rdb1(x)))
            return out * 0.2 + x

    class RRDBNet(torch.nn.Module):
        def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32):
            super().__init__()
            self.conv_first = _conv_block(torch, num_in_ch, num_feat)
            self.body = torch.nn.Sequential(*[RRDB(num_feat, num_grow_ch) for _ in range(num_block)])
            self.conv_body = _conv_block(torch, num_feat, num_feat)
            self.conv_up1 = _conv_block(torch, num_feat, num_feat)
            self.conv_up2 = _conv_block(torch, num_feat, num_feat)
            self.conv_hr = _conv_block(torch, num_feat, num_feat)
            self.conv_last = _conv_block(torch, num_feat, num_out_ch)
            self.lrelu = torch.nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            feat = self.conv_first(x)
            feat = feat + self.conv_body(self.body(feat))
            feat = self.lrelu(self.conv_up1(torch.nn.functional.interpolate(feat, scale_factor=2, mode="nearest")))
            feat = self.lrelu(self.conv_up2(torch.nn.functional.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    return RRDBNet()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_png(image_field: dict):
    from PIL import Image
    data = base64.b64decode(image_field["b64_json"], validate=True)
    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


def _to_tensor(torch, image):
    buffer = torch.frombuffer(bytearray(image.tobytes()), dtype=torch.uint8)
    return buffer.reshape(image.size[1], image.size[0], 3).permute(2, 0, 1).unsqueeze(0).float().div(255.0)


def _from_tensor(torch, tensor):
    from PIL import Image
    array = tensor.squeeze(0).clamp(0, 1).mul(255).round().to(torch.uint8)
    array = array.permute(1, 2, 0).contiguous().cpu()
    return Image.frombytes("RGB", (array.shape[1], array.shape[0]), bytes(array.numpy().tobytes()))


class RealEsrganRuntime:
    """Real-ESRGAN x4plus on GPU, tiled. `scale` (2 or 4) is applied after the 4x pass."""

    def __init__(self, snapshot: str, model: str, *, scale: int = 4, tile: int = TILE,
                 threads: int = 8, weight_sha256: str | None = None,
                 weight_bytes: int | None = None, precision: str = "fp16", **unused):
        if model not in UPSCALE_MODELS:
            raise UpscaleConfigurationError(f"upscale runtime does not serve {model}")
        if scale not in (2, NETWORK_SCALE):
            raise UpscaleConfigurationError("upscale scale must be 2 or 4")
        if type(tile) is not int or tile < 128:
            raise UpscaleConfigurationError("upscale tile must be an integer of at least 128 pixels")
        if precision not in ("fp16", "fp32"):
            raise UpscaleConfigurationError("upscale precision must be fp16 or fp32")
        import torch

        weights = Path(snapshot) / "RealESRGAN_x4plus.pth"
        if not weights.is_file():
            raise UpscaleConfigurationError(f"upscale weights missing: {weights}")
        if weight_bytes is not None and weights.stat().st_size != weight_bytes:
            raise UpscaleConfigurationError("upscale weight byte count mismatch")
        if weight_sha256 is not None and _file_sha256(weights) != weight_sha256:
            raise UpscaleConfigurationError("upscale weight SHA-256 mismatch")

        torch.set_num_threads(threads)
        self.torch, self.model, self.scale, self.tile = torch, model, scale, tile
        state = torch.load(weights, map_location="cpu")
        params = state.get("params_ema") or state.get("params")
        if not isinstance(params, dict):
            raise UpscaleConfigurationError("upscale checkpoint has no params_ema/params")
        self.net = _build_rrdbnet(torch)
        try:
            self.net.load_state_dict(params, strict=True)
        except RuntimeError as exc:
            raise UpscaleConfigurationError(f"upscale checkpoint does not match RRDBNet: {exc}") from exc
        self.dtype = torch.float16 if precision == "fp16" else torch.float32
        self.net.eval().to("cuda:0", dtype=self.dtype)
        self.weight_sha256 = weight_sha256

    def _tiled(self, image_tensor, cancel=None):
        torch = self.torch
        _, _, height, width = image_tensor.shape
        out = torch.empty(1, 3, height * NETWORK_SCALE, width * NETWORK_SCALE,
                          device=image_tensor.device, dtype=image_tensor.dtype)
        tiles = 0
        with torch.inference_mode():
            for y in range(0, height, self.tile):
                for x in range(0, width, self.tile):
                    if cancel is not None and cancel.is_set():
                        raise GenerationCancelled(f"caller went away; stopped before tile {tiles + 1}")
                    y0, x0 = max(0, y - TILE_PAD), max(0, x - TILE_PAD)
                    y1, x1 = min(height, y + self.tile + TILE_PAD), min(width, x + self.tile + TILE_PAD)
                    patch = image_tensor[:, :, y0:y1, x0:x1]
                    upsampled = self.net(patch)
                    ty, tx = min(height, y + self.tile), min(width, x + self.tile)
                    sy, sx = (y - y0) * NETWORK_SCALE, (x - x0) * NETWORK_SCALE
                    out[:, :, y * NETWORK_SCALE:ty * NETWORK_SCALE, x * NETWORK_SCALE:tx * NETWORK_SCALE] = \
                        upsampled[:, :,
                                  sy:sy + (ty - y) * NETWORK_SCALE,
                                  sx:sx + (tx - x) * NETWORK_SCALE]
                    tiles += 1
        return out, tiles

    def generate(self, request: dict, cancel=None) -> tuple[object, dict]:
        torch = self.torch
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        source = _decode_png(request["init_image"])
        tensor = _to_tensor(torch, source).to("cuda:0", dtype=self.dtype)
        free_cuda()
        upscaled, tiles = self._tiled(tensor, cancel)
        target = (source.size[0] * request["scale"], source.size[1] * request["scale"])
        if upscaled.shape[-1] != target[0] or upscaled.shape[-2] != target[1]:
            upscaled = torch.nn.functional.interpolate(upscaled, size=(target[1], target[0]),
                                                       mode="bicubic", align_corners=False, antialias=True)
        image = _from_tensor(torch, upscaled)
        if image.size != target:
            raise RuntimeError(f"upscaled image dimensions {image.size} do not match {target}")
        # Release the caching allocator's high-water mark: a 4x pass reserves ~13 GiB on the
        # biggest photos, and leaving that reserved would starve the card's other tenants.
        torch.cuda.empty_cache()
        metrics = {
            "task": "upscale",
            "upscale": {
                "scale": request["scale"],
                "network_scale": NETWORK_SCALE,
                "tile": self.tile,
                "tiles": tiles,
                "input_size": list(source.size),
                "output_size": list(image.size),
                "weight_sha256": self.weight_sha256,
            },
            "generation_s": round(time.monotonic() - started, 3),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }
        return image, metrics

    def close(self):
        self.net = None
        gc.collect()
        free_cuda()
