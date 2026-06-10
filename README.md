# Fal Image

A [Tesserae](https://github.com/dmellok/tesserae) widget that paints an AI-generated image full-bleed, using [Fal.ai](https://fal.ai) as the image-generation backend.

Brings your own API key. Defaults to Flux Schnell at ~$0.003/image.

## Install

Settings, Widgets, Browse community widgets, search "Fal Image", Install. Restart Tesserae when prompted.

Then in Settings, Plugins, Fal Image, paste your API key from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys).

## Cell options

- **Prompt** — describe what to generate.
- **Model** — Flux Schnell (cheap + fast, ~$0.003), Flux Dev (best quality, ~$0.025), Fast SDXL (~$0.01).
- **Aspect ratio** — `Auto` derives from the panel orientation, or pick explicitly (square, landscape 4:3 / 16:9, portrait 4:3 / 16:9).
- **Refresh cadence** — hourly, every 6 / 12 hours, or daily. The same prompt within a cadence bucket returns the cached image, so a multi-cell page refresh doesn't multiply spend.
- **Scale** — `Fit` (letterbox; full image visible) or `Fill` (crop to cover).
- **Show caption** — small overlay showing the prompt. Off by default.

## Cost model

| Cadence | Images/month | Flux Schnell | Flux Dev |
|---|---|---|---|
| Hourly | ~720 | ~$2.16 | ~$18.00 |
| Every 6h | ~120 | ~$0.36 | ~$3.00 |
| Every 12h | ~60 | ~$0.18 | ~$1.50 |
| Daily | 30 | ~$0.09 | ~$0.75 |

Per cell. Multiple cells with different prompts cost independently. Multiple cells with the same prompt and cadence share the cache (one image, one cost).

## How caching works

Within a single cadence bucket (e.g. hours 06:00 to 12:00 for a 6h cadence) every call with the same prompt, model, and image size returns the same cached image URL. When the bucket rolls over, the next render generates a fresh image. The seed is derived from `sha256(prompt + bucket_idx)` so even after a cache wipe the same prompt in the same window produces the same image.

## What you need

- A Fal.ai account and an API key from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys).
- Tesserae's renderer reaches out to `fal.run` (the API) and `fal.media` / `v3.fal.media` (where finished images are hosted) on each fresh generation. The widget's `requires:` block declares these so the network gate allows them.

## Caveats

- The Fal API hosts generated images at signed URLs that expire after some time. The widget caches the URL (not the bytes), so if a cached URL expires before the next bucket rollover, the cell will show a broken image until the next refresh. In practice the URLs outlive a 6h cadence window comfortably.
- Generation latency is 1-3 seconds for Schnell, up to 30 for Dev. The composer's existing last-good fallback shows the previously-rendered image while a slow generation is in flight.
- Models occasionally produce content that doesn't match the prompt or that the safety filter rejects. The widget surfaces the error message rather than the previous image, so you can adjust the prompt.

## Licence

MIT.
