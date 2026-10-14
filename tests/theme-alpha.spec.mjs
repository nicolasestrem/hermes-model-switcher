// Unit tests for the sticky-header surface colour rules (ui/src/theme.js).
//
// Codex review finding: only the two fully transparent spellings were rejected,
// so a PARTIALLY transparent host token (rgba(22, 24, 29, 0.5)) was accepted and
// rows stayed visible under the sticky header while scrolling.
import { test, expect } from "@playwright/test";
import { alphaOf, resolveOpaqueSurface, FALLBACK_BG } from "../ui/src/theme.js";

test("alphaOf reads every spelling Chromium uses for a colour", () => {
  expect(alphaOf("transparent")).toBe(0);
  expect(alphaOf("rgba(0, 0, 0, 0)")).toBe(0);
  expect(alphaOf("rgb(22, 24, 29)")).toBe(1);
  expect(alphaOf("rgba(22, 24, 29, 1)")).toBe(1);
  expect(alphaOf("rgba(22, 24, 29, 0.5)")).toBe(0.5);
  expect(alphaOf("rgb(22 24 29 / 50%)")).toBe(0.5);
  expect(alphaOf("rgb(22 24 29 / 100%)")).toBe(1);
  // the serialisation the real host actually produces
  expect(alphaOf("color(srgb 0.0156863 0.109804 0.109804)")).toBe(1);
  expect(alphaOf("color(srgb 0.1 0.1 0.1 / 0.5)")).toBe(0.5);
  expect(alphaOf("#16181d")).toBe(1);
  expect(alphaOf("rebeccapurple")).toBe(1);
  expect(alphaOf("hsl(210 20% 12%)")).toBe(1);
});

test("alphaOf reports unknown input instead of guessing", () => {
  expect(alphaOf("")).toBeNull();
  expect(alphaOf(undefined)).toBeNull();
  expect(alphaOf(null)).toBeNull();
  expect(alphaOf("not-a-colour")).toBeNull();
  expect(alphaOf("rgb(1, 2)")).toBeNull();
});

test("alphaOf reads the alpha channel of #RGBA / #RRGGBBAA hex", () => {
  // Codex review: every "#…" value returned 1, so a translucent hex host token
  // (e.g. #16181d80) was accepted and the sticky header stayed see-through.
  expect(alphaOf("#abc")).toBe(1); // 3-digit, opaque
  expect(alphaOf("#16181d")).toBe(1); // 6-digit, opaque
  expect(alphaOf("#16181dff")).toBe(1); // fully opaque 8-digit
  expect(alphaOf("#16181d00")).toBe(0); // fully transparent 8-digit
  expect(alphaOf("#16181d80")).toBeCloseTo(128 / 255, 5);
  expect(alphaOf("#16181d80")).toBeLessThan(1); // translucent, never treated as opaque
  expect(alphaOf("#abcd")).toBeCloseTo(13 / 15, 5); // #RGBA, case-insensitive
  expect(alphaOf("#ABCD")).toBeCloseTo(13 / 15, 5);
  // malformed lengths are unknown, never assumed opaque
  expect(alphaOf("#12345")).toBeNull();
  expect(alphaOf("#12")).toBeNull();
  expect(alphaOf("#gggggg")).toBeNull();
  expect(alphaOf("#16 18 1d")).toBeNull();
});

test("only a fully opaque surface is accepted, and never something unreadable", () => {
  // No document in Node: the helper must stay safe and return the solid colour.
  expect(resolveOpaqueSurface()).toBe(FALLBACK_BG);
  expect(FALLBACK_BG).toBe("#16181d");
});
