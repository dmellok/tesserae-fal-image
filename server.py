"""Fal Image widget, server-side fetch.

Calls Fal.ai's image generation API to produce one image per refresh
cadence bucket and caches the resulting URL. The same prompt within a
cadence window returns the cached URL, so a multi-cell page refresh
doesn't multiply spend.

API docs: https://fal.ai/models/fal-ai/flux/schnell/api

Cost model (Flux Schnell default):
    ~$0.003/image at 4 inference steps.
    Hourly cadence ~$2/mo per cell, 6h ~$0.36/mo, daily ~$0.09/mo.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://fal.run"
USER_AGENT = "tesserae-fal-image/0.1"
HTTP_TIMEOUT_S = 60.0
DEFAULT_MODEL = "fal-ai/flux/schnell"
DEFAULT_REFRESH_HOURS = 6

# Fal's named image_size presets. "auto" derives a preset from the
# panel orientation via ctx; everything else passes through unchanged.
_AUTO_BY_ASPECT = {
    "landscape": "landscape_4_3",
    "portrait": "portrait_4_3",
    "square": "square_hd",
}


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Per-render handler. Returns the client-side render context."""
    api_key = (settings.get("api_key") or "").strip()
    prompt = (options.get("prompt") or "").strip()
    model = (options.get("model") or DEFAULT_MODEL).strip()
    aspect_ratio = (options.get("aspect_ratio") or "auto").strip().lower()
    scale = (options.get("scale") or "fill").strip().lower()
    show_caption = bool(options.get("show_caption", False))
    try:
        refresh_hours = max(1, int(options.get("refresh_hours") or DEFAULT_REFRESH_HOURS))
    except (TypeError, ValueError):
        refresh_hours = DEFAULT_REFRESH_HOURS

    base = {
        "prompt": prompt,
        "model": model,
        "scale": scale,
        "show_caption": show_caption,
    }

    if not api_key:
        return {**base, "error": "Set your Fal.ai API key in Settings, Plugins, Fal Image."}
    if not prompt:
        return {**base, "error": "Add a prompt in this cell's settings."}

    image_size = _resolve_image_size(aspect_ratio, ctx)
    bucket_idx = int(time.time() // (refresh_hours * 3600))
    cache_key = _cache_key(model, prompt, image_size, bucket_idx)
    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_file = data_dir / f"{cache_key}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("image_url"):
                return {**base, **cached}
        except (json.JSONDecodeError, OSError):
            pass

    seed = _seed(prompt, bucket_idx)
    body: dict[str, Any] = {
        "prompt": prompt,
        "image_size": image_size,
        "seed": seed,
    }
    # Flux Dev's documented quality sweet spot. Schnell + SDXL each
    # default to a sensible step count server-side.
    if model == "fal-ai/flux/dev":
        body["num_inference_steps"] = 28

    try:
        payload = _fal_request(model, body, api_key)
    except urllib.error.HTTPError as err:
        return {**base, "error": f"Fal API {err.code}: {err.reason}"}
    except Exception as err:
        return {**base, "error": f"{type(err).__name__}: {err}"}

    image_url = _pick_image_url(payload)
    if not image_url:
        return {**base, "error": "Fal API returned no image URL."}

    result: dict[str, Any] = {
        "image_url": image_url,
        "generated_at": int(time.time()),
    }
    if isinstance(payload, dict) and "seed" in payload:
        result["seed"] = payload["seed"]
    with contextlib.suppress(OSError):
        cache_file.write_text(json.dumps(result), encoding="utf-8")
    return {**base, **result}


def _resolve_image_size(aspect_ratio: str, ctx: dict[str, Any]) -> str:
    """Map the user's aspect_ratio choice onto a Fal image_size preset.

    ``auto`` reads ``ctx['panel_w']`` / ``ctx['panel_h']`` and picks a
    preset matching the panel orientation. Explicit choices pass
    through so the user can override for portrait-on-landscape cells.
    """
    if aspect_ratio != "auto":
        return aspect_ratio
    try:
        pw = int(ctx.get("panel_w") or 0)
        ph = int(ctx.get("panel_h") or 0)
    except (TypeError, ValueError):
        pw = ph = 0
    if pw <= 0 or ph <= 0:
        return _AUTO_BY_ASPECT["landscape"]
    ratio = pw / ph
    if ratio > 1.25:
        return _AUTO_BY_ASPECT["landscape"]
    if ratio < 0.8:
        return _AUTO_BY_ASPECT["portrait"]
    return _AUTO_BY_ASPECT["square"]


def _cache_key(model: str, prompt: str, image_size: str, bucket_idx: int) -> str:
    raw = f"{model}|{prompt}|{image_size}|{bucket_idx}".encode()
    return "fal_" + hashlib.sha256(raw).hexdigest()[:16]


def _seed(prompt: str, bucket_idx: int) -> int:
    """Deterministic 32-bit seed. Same prompt + same bucket = same
    image even after a cache wipe."""
    raw = f"{prompt}|{bucket_idx}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def _fal_request(model: str, body: dict[str, Any], api_key: str) -> Any:
    url = f"{API_BASE}/{model.lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_image_url(payload: Any) -> str | None:
    """Extract the first image URL from a Fal response.

    Most Fal image models return ``{"images": [{"url": ...}], ...}``.
    A few return a single ``image`` dict instead, so try both shapes.
    """
    if not isinstance(payload, dict):
        return None
    images = payload.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str) and url:
                return url
    image = payload.get("image")
    if isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, str) and url:
            return url
    return None
