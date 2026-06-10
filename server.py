"""Fal Image widget, server-side fetch.

Calls Fal.ai's image generation API to produce one image per refresh
cadence bucket and caches the resulting URL. The same prompt within a
cadence window returns the cached URL, so a multi-cell page refresh
doesn't multiply spend.

Supported models (v0.4.0):
    fal-ai/flux/schnell         ~$0.003  (default, fast)
    fal-ai/hyper-sdxl           ~$0.003  (1-step SDXL)
    fal-ai/fast-sdxl            ~$0.01
    fal-ai/flux/dev             ~$0.025
    fal-ai/recraft-v3           ~$0.04   (crisp lines, dithers well)
    fal-ai/flux-pro/v1.1        ~$0.04   (best Flux tier)
    fal-ai/nano-banana          ~$0.039  (Gemini 2.5 Flash Image)
    fal-ai/nano-banana-2        ~$0.08   (newer Gemini-based)
    fal-ai/nano-banana-pro      ~$0.15   (premium Gemini-based)

API docs: https://fal.ai/models
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_BASE = "https://fal.run"
USER_AGENT = "tesserae-fal-image/0.4"
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

# Nano Banana family (Google Gemini 2.5+ Flash Image) accepts only
# ratio strings, not Fal's image_size presets. Map onto its vocab so
# the user's "Aspect ratio" choice means the same thing across models.
_NANO_ASPECT = {
    "square_hd": "1:1",
    "landscape_4_3": "4:3",
    "landscape_16_9": "16:9",
    "portrait_4_3": "3:4",
    "portrait_16_9": "9:16",
}

# Model categories (used to branch request body shape + which params apply).
_NANO_MODELS: frozenset[str] = frozenset(
    {"fal-ai/nano-banana", "fal-ai/nano-banana-2", "fal-ai/nano-banana-pro"}
)
_SDXL_MODELS: frozenset[str] = frozenset({"fal-ai/fast-sdxl", "fal-ai/hyper-sdxl"})
FLUX_DEV = "fal-ai/flux/dev"
RECRAFT_V3 = "fal-ai/recraft-v3"

# Flux + SDXL accept custom {width, height} but want them rounded to
# a multiple of 16, and clamped at 1536 each axis. Below ~256 the
# models produce garbage. These bounds match Fal's API and are well
# inside the renderer's panel limits.
_DIM_MIN = 256
_DIM_MAX = 1536
_DIM_STEP = 16

# Style presets prepended to the user's prompt before sending. ``none``
# leaves the prompt untouched. The strings are deliberately verbose so
# a vague prompt like "a cat" still gets a coherent rendering.
_STYLE_PRESETS: dict[str, str] = {
    "none": "",
    "oil_painting": "oil painting style, painterly brushstrokes, rich textures",
    "watercolor": "watercolor painting, soft washes, paper texture",
    "pencil_sketch": "graphite pencil sketch, hatched lines, paper grain",
    "pixel_art": "pixel art, 16-bit aesthetic, blocky shapes",
    "cyberpunk": "cyberpunk aesthetic, neon lights, rain-slick streets",
    "botanical": "vintage botanical illustration, scientific plate, fine ink lines",
    "bauhaus": "bauhaus geometric design, primary colors, hard edges, modernist",
    "risograph": "risograph print, limited two-color palette, paper grain, offset registration",
    "line_art": "minimal continuous line art, single line drawing, white background",
    "ukiyo_e": "ukiyo-e Japanese woodblock print, flat colors, bold outlines",
    "art_deco": "art deco style, geometric symmetry, gold accents, 1920s",
}

# Suffix appended when ``eink_friendly`` is on. These three hints
# steer the model away from busy, low-contrast outputs that turn into
# mud once dithered to a 6-colour Spectra panel.
_EINK_SUFFIX = ", high contrast, limited palette, simple composition"


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Per-render handler. Returns the client-side render context."""
    api_key = (settings.get("api_key") or "").strip()
    prompt_raw = (options.get("prompt") or "").strip()
    model = (options.get("model") or DEFAULT_MODEL).strip()
    aspect_ratio = (options.get("aspect_ratio") or "auto").strip().lower()
    scale = (options.get("scale") or "fill").strip().lower()
    style = (options.get("style") or "none").strip().lower()
    negative_prompt = (options.get("negative_prompt") or "").strip()
    eink_friendly = bool(options.get("eink_friendly", True))
    show_caption = bool(options.get("show_caption", False))
    try:
        refresh_hours = max(1, int(options.get("refresh_hours") or DEFAULT_REFRESH_HOURS))
    except (TypeError, ValueError):
        refresh_hours = DEFAULT_REFRESH_HOURS

    base = {
        "prompt": prompt_raw,  # The original user input, for caption display
        "model": model,
        "scale": scale,
        "show_caption": show_caption,
    }

    if not api_key:
        return {**base, "error": "Set your Fal.ai API key in Settings, Plugins, Fal Image."}
    if not prompt_raw:
        return {**base, "error": "Add a prompt in this cell's settings."}

    bucket_idx = int(time.time() // (refresh_hours * 3600))
    bucket_start = datetime.fromtimestamp(bucket_idx * refresh_hours * 3600, tz=UTC)
    # Build the actual prompt the API sees: rotation pick, placeholder
    # expansion (frozen at bucket start so the cache key is stable for
    # the whole bucket), style prepend, e-ink suffix append.
    final_prompt = _build_final_prompt(prompt_raw, style, eink_friendly, bucket_idx, bucket_start)
    # Show the rotation-picked + placeholder-expanded prompt in the
    # caption so the user sees what was actually generated, not the
    # raw multi-line template.
    base["prompt"] = final_prompt

    image_size = _resolve_image_size(aspect_ratio, ctx)
    custom_dims = _custom_dims_for(model, aspect_ratio, ctx)
    cache_key = _cache_key(model, final_prompt, image_size, bucket_idx, custom_dims)
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

    seed = _seed(final_prompt, bucket_idx)
    body = _build_body(model, final_prompt, image_size, seed, custom_dims, negative_prompt)

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


# -- prompt building ----------------------------------------------------


def _build_final_prompt(
    raw_prompt: str,
    style: str,
    eink_friendly: bool,
    bucket_idx: int,
    bucket_start: datetime,
) -> str:
    """Compose the prompt that's actually sent to Fal.

    Three steps, in order:

    1. Multi-prompt rotation: split on newlines, pick by ``bucket_idx``
       modulo the count. Same prompt within a bucket; rotates at
       bucket rollover.
    2. Placeholder expansion: ``{time_of_day}``, ``{season}``,
       ``{moon_phase}``, etc. Frozen at the bucket start so a 6h
       bucket gets a single coherent prompt for its whole window.
    3. Style preset prepend + e-ink suffix append.
    """
    selected = _pick_rotation(raw_prompt, bucket_idx)
    expanded = _expand_placeholders(selected, bucket_start)
    style_prefix = _STYLE_PRESETS.get(style, "")
    if style_prefix:
        expanded = f"{style_prefix}, {expanded}"
    if eink_friendly:
        expanded = expanded + _EINK_SUFFIX
    return expanded


def _pick_rotation(prompt: str, bucket_idx: int) -> str:
    """Pick one prompt from a newline-separated list, indexed by bucket."""
    parts = [p.strip() for p in prompt.splitlines() if p.strip()]
    if not parts:
        return prompt
    return parts[bucket_idx % len(parts)]


def _expand_placeholders(prompt: str, when: datetime) -> str:
    """Replace ``{time_of_day}`` etc. with values derived from ``when``.

    Empty / placeholder-free prompts pass through unchanged so the
    common case stays cheap. Unknown placeholders are left intact so
    a typo doesn't silently disappear from the prompt.
    """
    if not prompt or "{" not in prompt:
        return prompt
    # Render in local time so "time_of_day" matches the user's wall clock.
    local = when.astimezone()
    reps = {
        "{time_of_day}": _time_of_day(local),
        "{day_of_week}": local.strftime("%A"),
        "{date}": local.strftime("%B %d"),
        "{month}": local.strftime("%B"),
        "{season}": _season(local),
        "{hour}": local.strftime("%H"),
        "{year}": str(local.year),
        "{moon_phase}": _moon_phase(when),
    }
    for placeholder, value in reps.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def _time_of_day(local: datetime) -> str:
    h = local.hour
    if 5 <= h < 8:
        return "early morning"
    if 8 <= h < 12:
        return "morning"
    if 12 <= h < 14:
        return "midday"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 21:
        return "evening"
    if 21 <= h < 24:
        return "night"
    return "late night"


def _season(local: datetime) -> str:
    """Northern-hemisphere seasons by month. Southern users can drop
    explicit "{season}" placeholders and write the season into the
    prompt instead until we add a hemisphere setting."""
    m = local.month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "autumn"
    return "winter"


def _moon_phase(when: datetime) -> str:
    """Approximate moon phase from a reference new-moon timestamp +
    the 29.53059-day synodic month. Good to ~hours, more than enough
    for an ambient widget prompt."""
    # Reference new moon: 2000-01-06 18:14 UTC.
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
    days = (when - ref).total_seconds() / 86400.0
    phase = (days % 29.53059) / 29.53059
    if phase < 0.0625 or phase >= 0.9375:
        return "new moon"
    if phase < 0.1875:
        return "waxing crescent moon"
    if phase < 0.3125:
        return "first quarter moon"
    if phase < 0.4375:
        return "waxing gibbous moon"
    if phase < 0.5625:
        return "full moon"
    if phase < 0.6875:
        return "waning gibbous moon"
    if phase < 0.8125:
        return "last quarter moon"
    return "waning crescent moon"


# -- request body / capability flags ------------------------------------


def _build_body(
    model: str,
    prompt: str,
    image_size: str,
    seed: int,
    custom_dims: tuple[int, int] | None,
    negative_prompt: str,
) -> dict[str, Any]:
    """Build the per-model request body.

    Branches by model family:

    * **Nano Banana family**: ``aspect_ratio`` string + ``num_images``.
      No seed, no negative prompt, no image_size dict (the underlying
      Gemini API doesn't accept them).
    * **SDXL family** (Fast SDXL, Hyper SDXL): ``image_size`` (preset
      or {w, h}) + ``seed`` + optional ``negative_prompt``.
    * **Recraft V3**: same as SDXL.
    * **Flux family** (Schnell, Dev, Pro 1.1): ``image_size`` + ``seed``.
      Flux ignores ``negative_prompt`` so we don't send it. Flux Dev
      gets ``num_inference_steps=28`` for the quality sweet spot.
    """
    if model in _NANO_MODELS:
        return {
            "prompt": prompt,
            "aspect_ratio": _NANO_ASPECT.get(image_size, "4:3"),
            "num_images": 1,
        }
    body: dict[str, Any] = {"prompt": prompt, "seed": seed}
    if custom_dims is not None:
        w, h = custom_dims
        body["image_size"] = {"width": w, "height": h}
    else:
        body["image_size"] = image_size
    if negative_prompt and (model in _SDXL_MODELS or model == RECRAFT_V3):
        body["negative_prompt"] = negative_prompt
    if model == FLUX_DEV:
        body["num_inference_steps"] = 28
    return body


def _custom_dims_for(model: str, aspect_ratio: str, ctx: dict[str, Any]) -> tuple[int, int] | None:
    """Return rounded, clamped ``(w, h)`` from the cell's actual size,
    or ``None`` if we should fall back to a named preset.

    Returns ``None`` for:
      - Nano Banana family (API accepts only ratio strings)
      - Cells where the user explicitly picked an aspect_ratio preset
        (they're overriding the panel match deliberately)
      - Hosts that don't pass cell_w/cell_h (older Tesserae releases
        keep working unchanged)
    """
    if model in _NANO_MODELS:
        return None
    if aspect_ratio != "auto":
        return None
    try:
        cw = int(ctx.get("cell_w") or 0)
        ch = int(ctx.get("cell_h") or 0)
    except (TypeError, ValueError):
        return None
    if cw <= 0 or ch <= 0:
        return None
    return _round_dim(cw), _round_dim(ch)


def _round_dim(px: int) -> int:
    """Round to the nearest multiple of ``_DIM_STEP`` and clamp to the
    Fal-accepted ``[_DIM_MIN, _DIM_MAX]`` range."""
    px = max(_DIM_MIN, min(_DIM_MAX, px))
    return round(px / _DIM_STEP) * _DIM_STEP


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


def _cache_key(
    model: str,
    prompt: str,
    image_size: str,
    bucket_idx: int,
    custom_dims: tuple[int, int] | None,
) -> str:
    dims = f"{custom_dims[0]}x{custom_dims[1]}" if custom_dims else "preset"
    raw = f"{model}|{prompt}|{image_size}|{dims}|{bucket_idx}".encode()
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
