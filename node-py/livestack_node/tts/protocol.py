"""Closed native IndexTTS 2.5 control contract shared by PolyTTS and Harmony."""
import re

def validate_base_request(body):
    required = {"jobId", "ownerId", "text", "language", "voiceId", "referenceSha256", "controls"}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError("Closed managed speech request required")
    if not isinstance(body["jobId"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", body["jobId"]):
        raise ValueError("Invalid jobId")
    if not isinstance(body["ownerId"], str) or not body["ownerId"] or len(body["ownerId"]) > 192:
        raise ValueError("Installed owner prefix refused")
    if not isinstance(body["text"], str) or not 1 <= len(body["text"]) <= 4000:
        raise ValueError("Text must contain 1..4000 Unicode code points")
    if not isinstance(body["language"], str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)?", body["language"]):
        raise ValueError("Explicit native speech language required")
    if not isinstance(body["voiceId"], str) or not isinstance(body["referenceSha256"], str):
        raise ValueError("Explicit registered reference identity required")
    if not isinstance(body["controls"], dict):
        raise ValueError("Explicit native speech controls required")
    return body


def validate_request(body):
    validate_base_request(body)
    if body["language"] not in {"en", "zh", "ja", "es", "ar"}:
        raise ValueError("Unsupported IndexTTS2.5 language")
    c = body["controls"]
    required_controls = {"durationFactor", "emotionAlpha", "intervalSilenceMs", "maxTextTokensPerSegment", "seed"}
    if not isinstance(c, dict) or not required_controls <= c.keys() or set(c) - required_controls - {"emotionVector"}:
        raise ValueError("Closed explicit synthesis controls required")
    def number(x, lo, hi):
        import math
        return type(x) in (int, float) and math.isfinite(x) and lo <= x <= hi
    if not number(c["durationFactor"], .5, 2) or not number(c["emotionAlpha"], 0, 1):
        raise ValueError("Invalid duration/emotion control")
    if type(c["intervalSilenceMs"]) is not int or not 0 <= c["intervalSilenceMs"] <= 2000:
        raise ValueError("Invalid internal segment silence")
    if type(c["maxTextTokensPerSegment"]) is not int or not 20 <= c["maxTextTokensPerSegment"] <= 200:
        raise ValueError("Invalid internal segment token bound")
    if type(c["seed"]) is not int or not 0 <= c["seed"] < 2**32:
        raise ValueError("Invalid seed")
    if "emotionVector" in c and (not isinstance(c["emotionVector"], list) or len(c["emotionVector"]) != 8 or any(not number(x, 0, 1) for x in c["emotionVector"])):
        raise ValueError("Emotion vector must have exactly eight finite values in [0,1]")
    return body

