// Theme-token helpers for the sticky matrix header.
//
// The header MUST be opaque: rows scroll underneath it. `var(--surface-color,
// #16181d)` does not guarantee that — a var() fallback applies only when the
// variable is *undefined*, so a host theme defining the surface token as
// transparent, or PARTIALLY transparent (rgba(22, 24, 29, 0.5)), leaves the
// header see-through while scrolling.
export const FALLBACK_BG = "#16181d";

// Alpha of a computed colour, format-agnostically: Chromium serialises the same
// value as `rgb(...)`, `rgba(...)` or `color(srgb r g b / a)` depending on the
// source notation, so matching one spelling is not enough. `null` = unknown.
export function alphaOf(css) {
  if (typeof css !== "string") return null;
  const s = css.trim().toLowerCase();
  if (!s) return null;
  if (s === "transparent") return 0;
  const fn = s.match(/^(rgba?|hsla?)\(([^)]*)\)$/);
  if (fn) {
    const parts = fn[2].split(/[\s,/]+/).filter(Boolean);
    if (parts.length === 3) return 1;
    if (parts.length === 4) return alphaValue(parts[3]);
    return null;
  }
  const c = s.match(
    /^color\(\s*(?:srgb|srgb-linear|display-p3|a98-rgb|rec2020)\s+[^/\s]+\s+[^/\s]+\s+[^/\s]+\s*(?:\/\s*([^)\s]+))?\)$/,
  );
  if (c) return c[1] === undefined ? 1 : alphaValue(c[1]);
  if (s.startsWith("#") || /^[a-z]+$/.test(s)) return 1; // hex / named
  return null;
}

function alphaValue(token) {
  if (typeof token !== "string" || token === "") return null;
  if (token.endsWith("%")) {
    const pct = parseFloat(token);
    return Number.isFinite(pct) ? Math.min(Math.max(pct / 100, 0), 1) : null;
  }
  const n = parseFloat(token);
  return Number.isFinite(n) ? Math.min(Math.max(n, 0), 1) : null;
}

// The host's surface colour when it is fully opaque, otherwise our own solid
// colour. Called again whenever the host's theme attributes change.
export function resolveOpaqueSurface() {
  if (typeof document === "undefined" || !document.body) return FALLBACK_BG;
  const probe = document.createElement("div");
  probe.style.cssText =
    `position:absolute;visibility:hidden;pointer-events:none;background:var(--surface-color, ${FALLBACK_BG})`;
  document.body.appendChild(probe);
  let resolved = "";
  try {
    resolved = getComputedStyle(probe).backgroundColor || "";
  } finally {
    probe.remove();
  }
  const alpha = alphaOf(resolved);
  if (alpha === null) {
    // Unknown format: only one of the obvious transparent spellings is rejected,
    // and anything unreadable stays out of the header.
    return resolved && resolved !== "transparent" && resolved !== "rgba(0, 0, 0, 0)"
      ? resolved
      : FALLBACK_BG;
  }
  return alpha >= 1 && resolved ? resolved : FALLBACK_BG;
}
