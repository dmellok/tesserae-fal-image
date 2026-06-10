// Fal Image widget, client-side render.
// Full-bleed AI-generated image with optional prompt caption overlay.
// Two scale modes:
//   - fit:  letterbox; full image visible
//   - fill: crop to cover; no letterbox

export default function render(shadow, ctx) {
  const data = (ctx && ctx.data) || {};
  const scale = (data.scale || "fill").toLowerCase();
  const showCaption = data.show_caption === true;
  const error = data.error;
  shadow.innerHTML = layout(scale, showCaption, error, data);
}

function layout(scale, showCaption, error, data) {
  if (error) {
    return `
      ${styles()}
      <div class="frame">
        <div class="error">
          <p>${escapeHtml(error)}</p>
        </div>
      </div>
    `;
  }
  const url = data.image_url || "";
  const prompt = data.prompt || "";
  const captionVisible = showCaption && prompt;
  return `
    ${styles()}
    <div class="frame">
      <img class="art art--${scale}" src="${escapeHtml(url)}" alt="" />
      ${
        captionVisible
          ? `<figcaption class="caption">${escapeHtml(prompt)}</figcaption>`
          : ""
      }
    </div>
  `;
}

function styles() {
  return `
    <style>
      :host {
        display: block;
        width: 100%;
        height: 100%;
        overflow: hidden;
        background: var(--surface, #000);
      }
      .frame {
        position: relative;
        width: 100%;
        height: 100%;
      }
      .art {
        position: relative;
        width: 100%;
        height: 100%;
        display: block;
      }
      .art--fit  { object-fit: contain; }
      .art--fill { object-fit: cover; }
      .caption {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        padding: clamp(6px, 1.5cqmin, 12px) clamp(8px, 2cqmin, 16px);
        background: linear-gradient(180deg,
          rgba(0, 0, 0, 0) 0%,
          rgba(0, 0, 0, 0.55) 100%);
        color: #fff;
        font-family: var(--font-family, "Helvetica Neue", Helvetica, Arial, sans-serif);
        font-size: clamp(0.7em, 2.2cqmin, 0.95em);
        line-height: 1.3;
        font-weight: 500;
        z-index: 2;
        pointer-events: none;
        max-height: 25%;
        overflow: hidden;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
      }
      .error {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: clamp(8px, 3cqmin, 24px);
        text-align: center;
        color: var(--text-primary, #1B1A16);
        font-family: var(--font-family, "Helvetica Neue", Helvetica, Arial, sans-serif);
        font-size: clamp(0.8em, 2.6cqmin, 1.05em);
      }
      .error p { margin: 0; max-width: 36ch; }
    </style>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
