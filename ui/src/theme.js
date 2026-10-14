// Theme-token helpers for the sticky matrix header.
//
// The header MUST be opaque: rows scroll underneath it. `var(--surface-color,
// #16181d)` does not guarantee that — a var() fallback applies only when the
// variable is *undefined*, so a host theme defining the surface token as
// transparent, or PARTIALLY transparent (rgba(22, 24, 29, 0.5)), leaves the
// header see-through while scrolling.
export const FALLBACK_BG = "#16181d";
// Contrast-safe alternative for themes whose TEXT is dark (light theme): a
// fixed dark header on a light surface is worse than transparent — it is a
// black band. Chosen from the computed text colour, never from guesses about
// the host's palette.
export const FALLBACK_BG_LIGHT = "#f4f5f7";

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
  // Hex collapses to a single opaque alpha unless it carries the alpha channel:
  // #RGB/#RRGGBB are opaque, #RGBA/#RRGGBBAA embed it in the trailing nibble or
  // byte. Treating every "#…" as opaque would accept a translucent host token
  // (e.g. #16181d80) and leave the sticky header see-through.
  if (s.startsWith("#")) return hexAlpha(s);
  if (/^[a-z]+$/.test(s)) return 1; // named colour
  return null;
}

// Alpha of a #-prefixed colour: 1 for the 3/6-digit opaque forms, the trailing
// nibble/byte for the 4/8-digit forms, and `null` for any malformed length so
// an unknown value is never assumed opaque.
function hexAlpha(s) {
  const h = s.slice(1);
  if (!/^[0-9a-f]+$/.test(h)) return null;
  if (h.length === 3 || h.length === 6) return 1;
  if (h.length === 4) return clamp01(parseInt(h[3], 16) / 15); // #RGBA
  if (h.length === 8) return clamp01(parseInt(h.slice(6, 8), 16) / 255); // #RRGGBBAA
  return null;
}

function clamp01(n) {
  return Number.isFinite(n) ? Math.min(Math.max(n, 0), 1) : null;
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

// Relative luminance (WCAG) of a computed colour, or `null` when unreadable.
// Enough parsing for the rgb()/rgba()/color(srgb ...) spellings a browser
// actually serialises; anything else is unknown, and the caller must treat
// unknown as the dark-theme default (light text) rather than guess.
function luminanceOf(css) {
  const alpha = alphaOf(css);
  if (alpha === null) return null;
  const s = css.trim().toLowerCase();
  let r, g, b;
  const fn = s.match(/^(rgba?)\(([^)]*)\)$/);
  if (fn) {
    const parts = fn[2].split(/[\s,/]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const ch = [];
    for (let i = 0; i < 3; i++) {
      const tok = parts[i];
      const v = tok.endsWith("%") ? parseFloat(tok) / 100 : parseFloat(tok);
      if (!Number.isFinite(v)) return null;
      ch.push(tok.endsWith("%") ? v : v / 255);
    }
    [r, g, b] = ch;
  } else {
    const c = s.match(/^color\(\s*srgb\s+(\S+)\s+(\S+)\s+(\S+)/);
    if (!c) return null;
    r = parseFloat(c[1]); g = parseFloat(c[2]); b = parseFloat(c[3]);
    if (![r, g, b].every(Number.isFinite)) return null;
  }
  const lin = (v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

// Resolved computed value of a single custom property ("" when undefined),
// via the root element where the host writes its theme.
function tokenValue(name) {
  if (typeof document === "undefined" || !document.documentElement) return "";
  const cs = getComputedStyle(document.documentElement);
  return (cs.getPropertyValue(name) || "").trim();
}

// The fully opaque computed colour of `cssText`, or "" when undefined,
// translucent, or in any format whose alpha we cannot PROVE is 1.
function opaqueProbe(cssText) {
  if (typeof document === "undefined" || !document.body) return "";
  const probe = document.createElement("div");
  probe.style.cssText = `position:absolute;visibility:hidden;pointer-events:none;${cssText}`;
  document.body.appendChild(probe);
  let resolved = "";
  try {
    resolved = getComputedStyle(probe).backgroundColor || "";
  } finally {
    probe.remove();
  }
  const alpha = alphaOf(resolved);
  return alpha !== null && alpha >= 1 ? resolved : "";
}

// The host's surface colour when it is fully opaque; otherwise a solid
// fallback: the host's own opaque background (--background, then <body>) and,
// when even that is not provably opaque, a constant chosen from the computed
// TEXT luminance so the header always contrasts with what it renders above.
// An unknown-alpha value is NEVER passed through (an unparsed CSS Color 4
// colour may be translucent), and nothing readable but non-opaque either.
// Called again whenever the host's theme attributes change.
export function resolveOpaqueSurface() {
  if (typeof document === "undefined" || !document.body) return FALLBACK_BG;

  // Match all other surfaces: modern host token wins over stale legacy values.
  const modern = tokenValue("--background");
  if (modern) {
    const bg = opaqueProbe("background:var(--background)");
    if (bg) return bg;
  } else if (tokenValue("--surface-color")) {
    const surface = opaqueProbe("background:var(--surface-color)");
    if (surface) return surface;
  }
  // Fallback layer 2: the page's own background.
  const bodyBg = getComputedStyle(document.body).backgroundColor || "";
  if (alphaOf(bodyBg) === 1) return bodyBg;

  // Fallback layer 3: no opaque background anywhere — pick a constant that
  // contrasts with the text actually rendered in the tab.
  const probe = document.createElement("div");
  probe.style.cssText = "position:absolute;visibility:hidden;color:var(--midground, var(--text-color, #e6e8ee))";
  document.body.appendChild(probe);
  let fgCss;
  try { fgCss = getComputedStyle(probe).color; }
  finally { probe.remove(); }
  const lum = luminanceOf(fgCss);
  // Unknown text colour: default to the dark-theme fallback (light text) —
  // the Dashboard's default theme and every fixture in this repo are dark.
  return lum !== null && lum < 0.5 ? FALLBACK_BG_LIGHT : FALLBACK_BG;
}
