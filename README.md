# Fal Image

A [Tesserae](https://github.com/dmellok/tesserae) widget that paints an AI-generated image full-bleed, using [Fal.ai](https://fal.ai) as the image-generation backend.

Brings your own API key. Defaults to Flux Schnell at ~$0.003/image. Nine model options span from $0.003 (Flux Schnell, Hyper SDXL) to $0.15 (Nano Banana Pro). Style presets, contextual prompt placeholders (`{time_of_day}`, `{season}`, `{moon_phase}`, etc.), and newline-separated multi-prompt rotation.

## Install

Settings, Widgets, Browse community widgets, search "Fal Image", Install. Restart Tesserae when prompted.

Then in Settings, Plugins, Fal Image, paste your API key from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys).

## Cell options

- **Prompt** — multi-line textarea. One prompt per line rotates by bucket (line 1 on day 1, line 2 on day 2, etc.). Placeholders supported, see below.
- **Style preset** — None, Oil painting, Watercolour, Pencil sketch, Pixel art, Cyberpunk, Botanical illustration, Bauhaus, Risograph, Minimal line art, Ukiyo-e, Art deco. Prepended to the prompt before sending.
- **Model** — see the price table below.
- **Aspect ratio** — `Auto` matches the cell's exact dimensions (rounded for Flux + SDXL; snapped to the nearest ratio preset for Nano Banana). Or pick explicitly.
- **Refresh cadence** — hourly, every 6 / 12 hours, or daily.
- **Scale** — `Fit` (letterbox) or `Fill` (crop to cover).
- **Negative prompt** — what to avoid. Honoured by Fast SDXL, Hyper SDXL, and Recraft V3. Flux + Nano Banana ignore it.
- **E-ink-friendly suffix** — appends `, high contrast, limited palette, simple composition` so the image dithers cleanly. On by default.
- **Show caption** — small overlay showing the (final, expanded) prompt.

## Contextual prompt placeholders

Use any of these in your prompt; they're replaced with the bucket-start local time when the request fires:

| Placeholder | Example value |
|---|---|
| `{time_of_day}` | "early morning", "morning", "midday", "afternoon", "evening", "night", "late night" |
| `{day_of_week}` | "Wednesday" |
| `{date}` | "June 10" |
| `{month}` | "June" |
| `{season}` | "spring", "summer", "autumn", "winter" (northern hemisphere) |
| `{hour}` | "14" |
| `{year}` | "2026" |
| `{moon_phase}` | "waxing crescent moon" etc. |

Examples:

```
A {time_of_day} landscape in the style of Edward Hopper
```

```
A still life of {season} produce, botanical illustration
```

```
A {moon_phase} over a quiet harbour, ukiyo-e woodblock print
```

## Multi-prompt rotation

Paste several prompts, one per line. The widget picks one per refresh bucket via `bucket_idx % count`, so prompts rotate predictably:

```
A misty forest at {time_of_day}, oil painting
A neon-lit alley at {time_of_day}, cyberpunk
A botanical study of a {season} bloom
```

With a 24-hour cadence, that's a different aesthetic every day.

## Cost model

Per image:

| Model | Fal id | $/image |
|---|---|---|
| Flux Schnell | `fal-ai/flux/schnell` | ~$0.003 |
| Hyper SDXL | `fal-ai/hyper-sdxl` | ~$0.003 |
| Fast SDXL | `fal-ai/fast-sdxl` | ~$0.01 |
| Flux Dev | `fal-ai/flux/dev` | ~$0.025 |
| Recraft V3 | `fal-ai/recraft-v3` | ~$0.04 |
| Flux Pro 1.1 | `fal-ai/flux-pro/v1.1` | ~$0.04 |
| Nano Banana | `fal-ai/nano-banana` | ~$0.039 |
| Nano Banana 2 | `fal-ai/nano-banana-2` | ~$0.08 |
| Nano Banana Pro | `fal-ai/nano-banana-pro` | ~$0.15 |

Per cell per month at each cadence:

| Cadence | Images/mo | Schnell | Hyper SDXL | Fast SDXL | Flux Dev | Recraft V3 | Flux Pro 1.1 | NB | NB2 | NB Pro |
|---|---|---|---|---|---|---|---|---|---|---|
| Hourly | 720 | $2.16 | $2.16 | $7.20 | $18.00 | $28.80 | $28.80 | $28.08 | $57.60 | $108.00 |
| Every 6h | 120 | $0.36 | $0.36 | $1.20 | $3.00 | $4.80 | $4.80 | $4.68 | $9.60 | $18.00 |
| Every 12h | 60 | $0.18 | $0.18 | $0.60 | $1.50 | $2.40 | $2.40 | $2.34 | $4.80 | $9.00 |
| Daily | 30 | $0.09 | $0.09 | $0.30 | $0.75 | $1.20 | $1.20 | $1.17 | $2.40 | $4.50 |

Per cell. Multiple cells with different prompts cost independently. Multiple cells with the same prompt + cadence + dims share the cache (one image, one cost).

## How caching works

A "bucket" is a window of time set by **Refresh cadence**. Default 6h means buckets run 00:00–06:00, 06:00–12:00, and so on.

- **Within a bucket**: same prompt → same cached image URL. Multi-cell pages don't pay extra; a manual refresh doesn't pay extra.
- **At bucket rollover**: same prompt → a **fresh, different image** with a new seed.
- **Cache wipe within a bucket**: the seed is derived from `sha256(final_prompt + bucket_idx)`, so Flux + SDXL regenerate the identical image. Nano Banana is the exception (no seed param, non-deterministic) — a wipe + regen produces a different image even in the same bucket.

The dims used to generate the image are part of the cache key, so resizing a cell mid-bucket triggers one fresh image at the new size.

Placeholder expansion is frozen at the bucket-start timestamp, so the prompt the model sees stays stable across the whole bucket window even if the wall clock crosses a `time_of_day` boundary mid-bucket.

## What you need

- A Fal.ai account and an API key from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys).
- Tesserae's renderer reaches out to `fal.run` (the API) and `fal.media` / `v3.fal.media` (where finished images are hosted) on each fresh generation. The widget's `requires:` block declares these so the network gate allows them.

## Caveats

- Fal hosts generated images at signed URLs that expire after some hours. The widget caches the URL (not the bytes), so if a cached URL expires before the next bucket rollover, the cell will show a broken image until the next refresh. In practice the URLs outlive a 6h cadence window comfortably.
- Generation latency is 1-3 seconds for Schnell, up to 30 for Dev, longer for Nano Banana Pro at high quality.
- Models occasionally produce content that doesn't match the prompt or that the safety filter rejects. The widget surfaces the error message rather than the previous image, so you can adjust the prompt.
- The `{season}` placeholder uses the northern hemisphere. Southern users can write the season into the prompt directly until a hemisphere setting lands.

## Licence

MIT.
