"""Small host configuration seam; elapsed time and reminders belong to Node."""
import math
import json
from pathlib import Path


def review_policy(config):
    settings = config.get("review_policy")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("review_policy must be an object")
    profile = settings.get("profile", "standard")
    if profile not in ("standard", "deep"):
        raise ValueError("review_policy.profile must be standard or deep")
    result = {"profile": profile, "timezone": config.get("timezone", "Asia/Shanghai")}
    for name, default in (("remind_after_seconds", 600), ("conclude_after_seconds", 1200)):
        value = settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("review_policy." + name + " must be a finite non-negative number")
        result[name] = value
    if result["remind_after_seconds"] > 0 and result["conclude_after_seconds"] > 0 and result["conclude_after_seconds"] < result["remind_after_seconds"]:
        raise ValueError("review_policy.conclude_after_seconds must not precede remind_after_seconds")
    return result


def resume_elapsed(directory):
    """Carry active runtime only, never the wall-clock gap while a machine was off."""
    root = Path(directory).resolve()
    path = root / "workflow.json"
    if not path.is_file():
        return 0
    if not path.resolve().is_relative_to(root):
        raise ValueError("The prior timing metadata escapes its review directory")
    flow = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(flow, dict):
        raise ValueError("The prior timing metadata must be an object")
    timing = flow.get("review_timing") or {}
    values = [flow.get("seconds"), timing.get("elapsed_seconds") if isinstance(timing, dict) else None]
    return max([0] + [v for v in values if not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0])
