"""Exact-frame music gaps and splices. No models, resampling or global gain."""
from __future__ import annotations
import hashlib
import wave
from pathlib import Path
import numpy as np


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_pcm(path):
    with wave.open(str(path), "rb") as file:
        if file.getsampwidth() != 2 or file.getnchannels() != 2 or file.getcomptype() != "NONE":
            raise ValueError("Stereo PCM16 required")
        rate = file.getframerate()
        pcm = np.frombuffer(file.readframes(file.getnframes()), dtype="<i2").reshape(-1, 2).copy()
    return rate, pcm


def write_pcm(path, rate, pcm):
    with Path(path).open("xb") as stream:
        with wave.open(stream, "wb") as file:
            file.setnchannels(2)
            file.setsampwidth(2)
            file.setframerate(rate)
            file.writeframes(pcm.astype("<i2").tobytes())
    return {"sha256": digest(Path(path).read_bytes()), "frames": len(pcm), "sampleRate": rate,
            "pcmSha256": digest(pcm.astype("<i2").tobytes())}


def prepare_gap(source_path, start_frame, end_frame, replacement_frames, output_path):
    rate, original = read_pcm(source_path)
    if (any(type(v) is not int for v in (start_frame, end_frame, replacement_frames))
            or not 0 <= start_frame < end_frame <= len(original) or replacement_frames <= 0):
        raise ValueError("Exact bounded gap frames required")
    out = np.concatenate((original[:start_frame], np.zeros((replacement_frames, 2), dtype="<i2"), original[end_frame:]))
    result = write_pcm(output_path, rate, out)
    return {"operation": "prepare-variable-length-silent-gap", "sourceSha256": digest(Path(source_path).read_bytes()),
            "sourceStartFrame": start_frame, "sourceEndFrame": end_frame, "replacementFrames": replacement_frames,
            "shiftFrames": replacement_frames - (end_frame - start_frame), "result": result,
            "sourcePrefixPcmSha256": digest(original[:start_frame].tobytes()),
            "sourceSuffixPcmSha256": digest(original[end_frame:].tobytes()),
            "suffixMovedWithoutTimeStretch": True, "inferencePerformed": False}


def splice(source_path, candidate_path, start_frame, end_frame, output_path, blend_frames):
    rate, original = read_pcm(source_path)
    candidate_rate, candidate = read_pcm(candidate_path)
    if candidate_rate != rate:
        raise ValueError("Explicit native clock conversion required before splice")
    if (any(type(v) is not int for v in (start_frame, end_frame, blend_frames))
            or not 0 <= start_frame < end_frame <= len(original) or len(candidate) < end_frame
            or not 0 <= blend_frames <= (end_frame - start_frame) // 2):
        raise ValueError("Exact bounded splice frames required")
    weights = np.ones((end_frame - start_frame, 1))
    if blend_frames:
        if start_frame:
            weights[:blend_frames, 0] = np.linspace(0, 1, blend_frames)
        if end_frame < len(original):
            weights[-blend_frames:, 0] = np.linspace(1, 0, blend_frames)
    out = original.copy()
    mixed = original[start_frame:end_frame].astype(float) * (1 - weights) + candidate[start_frame:end_frame].astype(float) * weights
    out[start_frame:end_frame] = np.rint(mixed).astype("<i2")
    assert np.array_equal(out[:start_frame], original[:start_frame])
    assert np.array_equal(out[end_frame:], original[end_frame:])
    result = write_pcm(output_path, rate, out)
    return {"operation": "exact-frame-native-music-splice", "sourceSha256": digest(Path(source_path).read_bytes()),
            "candidateSha256": digest(Path(candidate_path).read_bytes()), "startFrame": start_frame,
            "endFrame": end_frame, "blendFramesInsideMask": blend_frames, "outsidePcmExact": True,
            "frozenPrefixPcmSha256": digest(out[:start_frame].tobytes()),
            "frozenSuffixPcmSha256": digest(out[end_frame:].tobytes()), "result": result,
            "replacementGain": 1, "globalNormalization": False, "timeStretch": False}
