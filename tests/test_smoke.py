"""Smoke tests for the Fal Image widget.

These don't hit the live Fal API. They patch ``_fal_request`` and
exercise the request-building, caching, error-handling, placeholder
expansion, multi-prompt rotation, and per-model body shape. Live
integration goes through the renderer's headless Chromium, not pytest.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Tesserae's pytest config loads server.py via its file path; the
# module name resolves to ``server`` for the smoke test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server

_SAMPLE_OK: dict[str, Any] = {
    "images": [
        {
            "url": "https://v3.fal.media/files/abc/def.jpg",
            "width": 1024,
            "height": 768,
            "content_type": "image/jpeg",
        }
    ],
    "timings": {"inference": 1.2},
    "seed": 42,
    "has_nsfw_concepts": [False],
    "prompt": "test prompt",
}


def _ctx(
    tmp_path: Path,
    *,
    panel_w: int = 1200,
    panel_h: int = 800,
    cell_w: int = 0,
    cell_h: int = 0,
) -> dict[str, Any]:
    return {
        "panel_w": panel_w,
        "panel_h": panel_h,
        "cell_w": cell_w,
        "cell_h": cell_h,
        "preview": False,
        "data_dir": str(tmp_path),
    }


# -- core fetch flow ----------------------------------------------------


def test_fetch_happy_path_returns_image_url(tmp_path: Path) -> None:
    """Valid key + prompt + working API => image_url + metadata."""
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        out = server.fetch(
            options={
                "prompt": "a serene mountain",
                "model": "fal-ai/flux/schnell",
                "eink_friendly": False,
            },
            settings={"api_key": "test-key"},
            ctx=_ctx(tmp_path),
        )
    assert "error" not in out
    assert out["image_url"] == "https://v3.fal.media/files/abc/def.jpg"
    assert out["prompt"] == "a serene mountain"
    assert out["model"] == "fal-ai/flux/schnell"
    assert out["scale"] == "fill"
    assert out["show_caption"] is False
    assert "generated_at" in out
    sent_body = mock.call_args.args[1]
    assert sent_body["prompt"] == "a serene mountain"
    assert isinstance(sent_body["seed"], int)


def test_fetch_missing_api_key_surfaces_friendly_error(tmp_path: Path) -> None:
    with patch.object(server, "_fal_request") as mock:
        out = server.fetch(
            options={"prompt": "anything"},
            settings={"api_key": ""},
            ctx=_ctx(tmp_path),
        )
    assert "error" in out
    assert "API key" in out["error"]
    mock.assert_not_called()


def test_fetch_missing_prompt_surfaces_friendly_error(tmp_path: Path) -> None:
    with patch.object(server, "_fal_request") as mock:
        out = server.fetch(
            options={"prompt": "   "},
            settings={"api_key": "test-key"},
            ctx=_ctx(tmp_path),
        )
    assert "error" in out
    assert "prompt" in out["error"].lower()
    mock.assert_not_called()


def test_fetch_caches_within_bucket(tmp_path: Path) -> None:
    """Two fetches with the same prompt within the same cadence bucket
    should only hit the API once. The second returns the cached URL."""
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        opts = {"prompt": "p", "refresh_hours": "6"}
        server.fetch(options=opts, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
        server.fetch(options=opts, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
    assert mock.call_count == 1


def test_fetch_different_prompts_dont_share_cache(tmp_path: Path) -> None:
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        server.fetch(options={"prompt": "a"}, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
        server.fetch(options={"prompt": "b"}, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
    assert mock.call_count == 2


def test_fetch_handles_api_error_gracefully(tmp_path: Path) -> None:
    def boom(model: str, body: dict[str, Any], api_key: str) -> Any:
        del model, body, api_key
        raise RuntimeError("connection refused")

    with patch.object(server, "_fal_request", side_effect=boom):
        out = server.fetch(
            options={"prompt": "x"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert "error" in out
    assert "RuntimeError" in out["error"]
    assert not any(tmp_path.glob("fal_*.json"))


def test_fetch_handles_empty_image_list(tmp_path: Path) -> None:
    with patch.object(server, "_fal_request", return_value={"images": []}):
        out = server.fetch(
            options={"prompt": "x"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert "error" in out


# -- aspect resolution + custom dims ------------------------------------


def test_auto_aspect_picks_landscape_for_wide_panel(tmp_path: Path) -> None:
    out = server._resolve_image_size("auto", _ctx(tmp_path, panel_w=1600, panel_h=900))
    assert out == "landscape_4_3"


def test_auto_aspect_picks_portrait_for_tall_panel(tmp_path: Path) -> None:
    out = server._resolve_image_size("auto", _ctx(tmp_path, panel_w=600, panel_h=1200))
    assert out == "portrait_4_3"


def test_auto_aspect_picks_square_for_squarish_panel(tmp_path: Path) -> None:
    out = server._resolve_image_size("auto", _ctx(tmp_path, panel_w=1000, panel_h=1000))
    assert out == "square_hd"


def test_explicit_aspect_ratio_passes_through(tmp_path: Path) -> None:
    out = server._resolve_image_size("portrait_16_9", _ctx(tmp_path, panel_w=1600, panel_h=900))
    assert out == "portrait_16_9"


def test_cell_dims_drive_custom_image_size_for_flux(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(model: str, body: dict[str, Any], api_key: str) -> Any:
        del model, api_key
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x", "aspect_ratio": "auto", "eink_friendly": False},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path, cell_w=387, cell_h=289),
        )
    assert captured.get("image_size") == {"width": 384, "height": 288}


def test_cell_dims_clamped_to_fal_range(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(model: str, body: dict[str, Any], api_key: str) -> Any:
        del model, api_key
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x", "aspect_ratio": "auto", "eink_friendly": False},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path, cell_w=80, cell_h=80),
        )
    assert captured["image_size"] == {"width": 256, "height": 256}
    captured.clear()
    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x2", "aspect_ratio": "auto", "eink_friendly": False},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path, cell_w=4000, cell_h=4000),
        )
    assert captured["image_size"] == {"width": 1536, "height": 1536}


def test_round_dim_picks_nearest_multiple_of_16() -> None:
    assert server._round_dim(384) == 384
    assert server._round_dim(385) == 384
    assert server._round_dim(391) == 384
    assert server._round_dim(396) == 400
    assert server._round_dim(400) == 400


# -- per-model body shape -----------------------------------------------


def test_flux_dev_gets_28_inference_steps(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(model: str, body: dict[str, Any], api_key: str) -> Any:
        del model, api_key
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x", "model": "fal-ai/flux/dev"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert captured.get("num_inference_steps") == 28


def test_flux_schnell_omits_inference_steps(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(model: str, body: dict[str, Any], api_key: str) -> Any:
        del model, api_key
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x", "model": "fal-ai/flux/schnell"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert "num_inference_steps" not in captured


def test_nano_banana_variants_use_aspect_ratio_string(tmp_path: Path) -> None:
    """Nano Banana, NB2, and NB Pro all use aspect_ratio strings (not
    image_size dicts), have no seed, and accept no negative_prompt."""
    for model in ("fal-ai/nano-banana", "fal-ai/nano-banana-2", "fal-ai/nano-banana-pro"):
        captured: dict[str, Any] = {}

        def capture(_model: str, body: dict[str, Any], api_key: str, _c=captured) -> Any:
            del _model, api_key
            _c.update(body)
            return _SAMPLE_OK

        with patch.object(server, "_fal_request", side_effect=capture):
            server.fetch(
                options={
                    "prompt": "x",
                    "model": model,
                    "aspect_ratio": "landscape_16_9",
                    "negative_prompt": "blurry",
                },
                settings={"api_key": "k"},
                ctx=_ctx(tmp_path),
            )
        assert captured.get("aspect_ratio") == "16:9", model
        assert "image_size" not in captured, model
        assert "seed" not in captured, model
        assert "negative_prompt" not in captured, model
        assert captured.get("num_images") == 1, model


def test_sdxl_models_accept_negative_prompt(tmp_path: Path) -> None:
    """Fast SDXL + Hyper SDXL honour negative_prompt."""
    for model in ("fal-ai/fast-sdxl", "fal-ai/hyper-sdxl"):
        captured: dict[str, Any] = {}

        def capture(_model: str, body: dict[str, Any], api_key: str, _c=captured) -> Any:
            del _model, api_key
            _c.update(body)
            return _SAMPLE_OK

        with patch.object(server, "_fal_request", side_effect=capture):
            server.fetch(
                options={"prompt": "x", "model": model, "negative_prompt": "blurry, ugly"},
                settings={"api_key": "k"},
                ctx=_ctx(tmp_path),
            )
        assert captured.get("negative_prompt") == "blurry, ugly", model


def test_recraft_v3_accepts_negative_prompt(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(_model: str, body: dict[str, Any], api_key: str) -> Any:
        del _model, api_key
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "x", "model": "fal-ai/recraft-v3", "negative_prompt": "watermark"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert captured.get("negative_prompt") == "watermark"


def test_flux_drops_negative_prompt(tmp_path: Path) -> None:
    """Flux models ignore negative_prompt server-side; we don't send it."""
    for model in ("fal-ai/flux/schnell", "fal-ai/flux/dev", "fal-ai/flux-pro/v1.1"):
        captured: dict[str, Any] = {}

        def capture(_model: str, body: dict[str, Any], api_key: str, _c=captured) -> Any:
            del _model, api_key
            _c.update(body)
            return _SAMPLE_OK

        with patch.object(server, "_fal_request", side_effect=capture):
            server.fetch(
                options={"prompt": "x", "model": model, "negative_prompt": "blurry"},
                settings={"api_key": "k"},
                ctx=_ctx(tmp_path),
            )
        assert "negative_prompt" not in captured, model


# -- prompt composition (style, rotation, placeholders, suffix) ---------


def test_style_preset_prepended_to_prompt(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(_m: str, body: dict[str, Any], _k: str) -> Any:
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "a cat", "style": "ukiyo_e", "eink_friendly": False},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    sent = captured["prompt"]
    assert sent.startswith("ukiyo-e")
    assert "a cat" in sent


def test_style_none_leaves_prompt_unchanged(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(_m: str, body: dict[str, Any], _k: str) -> Any:
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "a cat", "style": "none", "eink_friendly": False},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert captured["prompt"] == "a cat"


def test_eink_suffix_appended_when_enabled(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(_m: str, body: dict[str, Any], _k: str) -> Any:
        captured.update(body)
        return _SAMPLE_OK

    with patch.object(server, "_fal_request", side_effect=capture):
        server.fetch(
            options={"prompt": "a cat", "eink_friendly": True},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert captured["prompt"].endswith("high contrast, limited palette, simple composition")


def test_multi_prompt_rotation_picks_by_bucket() -> None:
    """Newline-separated prompts rotate by ``bucket_idx % count``."""
    prompt = "morning\nafternoon\nevening"
    assert server._pick_rotation(prompt, 0) == "morning"
    assert server._pick_rotation(prompt, 1) == "afternoon"
    assert server._pick_rotation(prompt, 2) == "evening"
    assert server._pick_rotation(prompt, 3) == "morning"


def test_multi_prompt_rotation_skips_blank_lines() -> None:
    """Blank lines in the prompt are ignored so users can space out."""
    prompt = "first\n\nsecond\n  \nthird"
    assert server._pick_rotation(prompt, 0) == "first"
    assert server._pick_rotation(prompt, 1) == "second"
    assert server._pick_rotation(prompt, 2) == "third"


def test_single_line_prompt_skips_rotation() -> None:
    assert server._pick_rotation("just one prompt", 99) == "just one prompt"


# -- placeholder expansion ----------------------------------------------


def test_time_of_day_at_morning() -> None:
    # _time_of_day reads local.hour directly; passing hour=10 is enough,
    # the function doesn't care about the datetime's tz.
    assert server._time_of_day(datetime(2026, 6, 10, 10, 0)) == "morning"


def test_time_of_day_at_late_night() -> None:
    assert server._time_of_day(datetime(2026, 6, 10, 2, 0)) == "late night"


def test_time_of_day_buckets_full_range() -> None:
    """Smoke each band so the boundary logic doesn't quietly drift."""
    assert server._time_of_day(datetime(2026, 6, 10, 6, 0)) == "early morning"
    assert server._time_of_day(datetime(2026, 6, 10, 10, 0)) == "morning"
    assert server._time_of_day(datetime(2026, 6, 10, 13, 0)) == "midday"
    assert server._time_of_day(datetime(2026, 6, 10, 16, 0)) == "afternoon"
    assert server._time_of_day(datetime(2026, 6, 10, 19, 0)) == "evening"
    assert server._time_of_day(datetime(2026, 6, 10, 22, 0)) == "night"
    assert server._time_of_day(datetime(2026, 6, 10, 3, 0)) == "late night"


def test_season_north_hemisphere_summer() -> None:
    assert server._season(datetime(2026, 7, 10, 12, 0)) == "summer"


def test_season_north_hemisphere_winter() -> None:
    assert server._season(datetime(2026, 1, 10, 12, 0)) == "winter"


def test_moon_phase_returns_one_of_known_phases() -> None:
    """Pick a few known dates and verify the function returns plausible
    phase names. Astronomical accuracy isn't the goal; consistent
    deterministic output is."""
    out = server._moon_phase(datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
    assert out in (
        "new moon",
        "waxing crescent moon",
        "first quarter moon",
        "waxing gibbous moon",
        "full moon",
        "waning gibbous moon",
        "last quarter moon",
        "waning crescent moon",
    )


def test_expand_placeholders_passes_through_when_no_braces() -> None:
    out = server._expand_placeholders("plain prompt", datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
    assert out == "plain prompt"


def test_expand_placeholders_substitutes_time_of_day() -> None:
    when = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    out = server._expand_placeholders("a {time_of_day} landscape", when)
    # The actual word depends on the user's local timezone; just check
    # the placeholder is gone.
    assert "{time_of_day}" not in out
    assert "landscape" in out


def test_expand_placeholders_substitutes_day_of_week() -> None:
    when = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # Wednesday
    out = server._expand_placeholders("the {day_of_week} feeling", when)
    assert "{day_of_week}" not in out
    # June 10, 2026 is a Wednesday in most timezones (could be Tue/Thu
    # depending on the machine's TZ offset, but both are plausible).
    assert any(d in out for d in ("Monday", "Tuesday", "Wednesday", "Thursday"))


def test_expand_placeholders_unknown_token_passes_through() -> None:
    """An unknown placeholder stays in the prompt so typos don't
    silently disappear. The user sees the literal token and notices."""
    out = server._expand_placeholders("a {made_up} landscape", datetime(2026, 6, 10, tzinfo=UTC))
    assert "{made_up}" in out


def test_build_final_prompt_chains_rotation_expansion_style_suffix() -> None:
    """End-to-end: rotation -> placeholder expansion -> style prepend
    -> e-ink suffix. Verifies all four steps run, in order."""
    raw = "scene A\nscene B at {time_of_day}"
    when = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    # bucket_idx=1 picks the second line.
    out = server._build_final_prompt(
        raw, "oil_painting", eink_friendly=True, bucket_idx=1, bucket_start=when
    )
    assert out.startswith("oil painting style")
    assert "scene B" in out
    assert "{time_of_day}" not in out
    assert out.endswith(", high contrast, limited palette, simple composition")
