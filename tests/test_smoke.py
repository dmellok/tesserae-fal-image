"""Smoke tests for the Fal Image widget.

These don't hit the live Fal API. They patch ``_fal_request`` and
exercise the request-building, caching, error-handling, and
panel-aware aspect-ratio resolution. Live integration goes through
the renderer's headless Chromium, not pytest.
"""

from __future__ import annotations

import json
import sys
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


def _ctx(tmp_path: Path, *, panel_w: int = 1200, panel_h: int = 800) -> dict[str, Any]:
    return {
        "panel_w": panel_w,
        "panel_h": panel_h,
        "preview": False,
        "data_dir": str(tmp_path),
    }


def test_fetch_happy_path_returns_image_url(tmp_path: Path) -> None:
    """Valid key + prompt + working API => image_url + metadata."""
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        out = server.fetch(
            options={"prompt": "a serene mountain", "model": "fal-ai/flux/schnell"},
            settings={"api_key": "test-key"},
            ctx=_ctx(tmp_path),
        )
    assert "error" not in out
    assert out["image_url"] == "https://v3.fal.media/files/abc/def.jpg"
    assert out["prompt"] == "a serene mountain"
    assert out["model"] == "fal-ai/flux/schnell"
    assert out["scale"] == "fill"  # default
    assert out["show_caption"] is False  # default
    assert "generated_at" in out
    # The request body should include the prompt + a derived seed.
    sent_body = mock.call_args.args[1]
    assert sent_body["prompt"] == "a serene mountain"
    assert isinstance(sent_body["seed"], int)


def test_fetch_missing_api_key_surfaces_friendly_error(tmp_path: Path) -> None:
    """No api_key => helpful onboarding error, no network call."""
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
    """No prompt => helpful error, no network call."""
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
    """Different prompts -> different cache keys -> independent API calls."""
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        server.fetch(options={"prompt": "a"}, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
        server.fetch(options={"prompt": "b"}, settings={"api_key": "k"}, ctx=_ctx(tmp_path))
    assert mock.call_count == 2


def test_fetch_handles_api_error_gracefully(tmp_path: Path) -> None:
    """fal_request raises -> error surfaces, no crash, no cache write."""

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
    # No cache file written on error
    assert not any(tmp_path.glob("fal_*.json"))


def test_fetch_handles_empty_image_list(tmp_path: Path) -> None:
    """API responds 200 but with no images -> friendly error."""
    with patch.object(server, "_fal_request", return_value={"images": []}):
        out = server.fetch(
            options={"prompt": "x"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert "error" in out


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
    """An explicit preset should pass through unchanged regardless of
    panel dims, so a portrait cell on a landscape panel still gets a
    portrait image."""
    out = server._resolve_image_size("portrait_16_9", _ctx(tmp_path, panel_w=1600, panel_h=900))
    assert out == "portrait_16_9"


def test_seed_is_deterministic() -> None:
    """Same prompt + same bucket = same seed, every time."""
    assert server._seed("mountain", 100) == server._seed("mountain", 100)
    assert server._seed("mountain", 100) != server._seed("mountain", 101)
    assert server._seed("mountain", 100) != server._seed("ocean", 100)


def test_flux_dev_gets_28_inference_steps(tmp_path: Path) -> None:
    """Flux Dev's documented quality sweet spot is 28 steps; ensure
    we override the server-side default for that model."""
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
    """Schnell uses the server-side default (4 steps); don't override."""
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


def test_corrupt_cache_file_falls_through_to_api(tmp_path: Path) -> None:
    """A garbage cache file shouldn't kill the widget; the next fetch
    should silently rewrite it after a successful API call."""
    # Pre-pollute the cache with junk.
    junk = tmp_path / "fal_deadbeef.json"
    junk.write_text("not json", encoding="utf-8")
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK) as mock:
        out = server.fetch(
            options={"prompt": "fresh"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    assert "error" not in out
    assert out["image_url"] == "https://v3.fal.media/files/abc/def.jpg"
    assert mock.call_count == 1


def test_pick_image_url_handles_legacy_single_image_shape() -> None:
    """Some Fal models return ``image: {url}`` instead of ``images: [{url}]``."""
    payload = {"image": {"url": "https://v3.fal.media/x.png"}}
    assert server._pick_image_url(payload) == "https://v3.fal.media/x.png"


def test_pick_image_url_returns_none_on_garbage() -> None:
    assert server._pick_image_url(None) is None
    assert server._pick_image_url({}) is None
    assert server._pick_image_url({"images": []}) is None
    assert server._pick_image_url({"images": [{}]}) is None


def test_cache_payload_persisted_to_disk(tmp_path: Path) -> None:
    """After a successful fetch, the JSON cache file should be on disk
    and parseable. (Validates the write path.)"""
    with patch.object(server, "_fal_request", return_value=_SAMPLE_OK):
        server.fetch(
            options={"prompt": "persist"},
            settings={"api_key": "k"},
            ctx=_ctx(tmp_path),
        )
    written = list(tmp_path.glob("fal_*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["image_url"] == "https://v3.fal.media/files/abc/def.jpg"
