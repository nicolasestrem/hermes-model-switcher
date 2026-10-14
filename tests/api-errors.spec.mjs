// Unit tests for the error-unwrapping rules in ui/src/api.js.
//
// These run in Node (no browser): errorMessage is pure. It decides whether the
// user sees the engine's actionable sentence or a bare "HTTP 409" — the exact
// difference between "the apply button does nothing" and a message that says
// what to fix.
import { test, expect } from "@playwright/test";
import { errorMessage } from "../ui/src/api.js";

test("unwraps a FastAPI detail payload (the host's real refusal shape)", () => {
  const body = {
    detail: {
      ok: false,
      blocked: true,
      error: "apply blocked: the canonical route was rejected",
      reasons: ["Nous Portal is not connected: no API key or login was found for it."],
    },
  };
  expect(errorMessage(body, 409)).toBe(
    "apply blocked: the canonical route was rejected — Nous Portal is not connected: no API key or login was found for it.",
  );
});

test("unwraps a flat body and joins several reasons", () => {
  expect(errorMessage({ error: "preview failed", reasons: ["a", "b"] }, 400))
    .toBe("preview failed — a; b");
});

test("a string-valued reasons field does not throw", () => {
  // `.join` on a string would throw inside the error path itself, swallowing
  // the real message and leaving the user with a broken status line.
  expect(errorMessage({ error: "boom", reasons: "single reason" }, 500))
    .toBe("boom — single reason");
  expect(errorMessage({ detail: { error: "boom", reasons: "nested reason" } }, 500))
    .toBe("boom — nested reason");
});

test("a string detail does not throw and falls back to the status", () => {
  expect(errorMessage({ detail: "Not Found" }, 404)).toBe("HTTP 404");
  expect(errorMessage({ detail: "Not Found", error: "flat error" }, 404)).toBe("flat error");
});

test("non-scalar error and reasons cannot print [object Object]", () => {
  // These shapes reached the visible status line as literal "[object Object]"
  // before: the ternary kept an object and the template string stringified it.
  expect(errorMessage({ error: { nested: true } }, 500)).toBe("HTTP 500");
  expect(errorMessage({ error: "boom", reasons: [{ a: 1 }] }, 500)).toBe("boom");
  expect(errorMessage({ detail: { error: { a: 1 }, reasons: { b: 2 } } }, 409)).toBe("HTTP 409");
  // scalars still render, including a numeric or boolean reason
  expect(errorMessage({ error: "boom", reasons: [0, "real reason", null, {}, false] }, 400))
    .toBe("boom — 0; real reason; false");
});

test("a null/absent body falls back to the status and never says ok", () => {
  expect(errorMessage(null, 502)).toBe("HTTP 502");
  expect(errorMessage(undefined, 401)).toBe("HTTP 401");
  expect(errorMessage({}, 500)).toBe("HTTP 500");
  expect(errorMessage({ ok: false }, 200)).toBe("HTTP 200");
});

test("unwraps detail.blocked per-profile/slot reasons so the user knows WHICH slot", () => {
  const body = {
    detail: {
      ok: false,
      error: "preview blocked for some profiles",
      blocked: [
        { profileId: "default", slotId: "auxiliary.goal_judge", reason: "typed-decision model required" },
        { profileId: "research", reason: "profile is read-only" },
      ],
    },
  };
  expect(errorMessage(body, 422)).toBe(
    "preview blocked for some profiles — default/auxiliary.goal_judge: typed-decision model required; research: profile is read-only",
  );
});

test("blocked reasons combine with reasons and surface without an error summary", () => {
  expect(errorMessage({ error: "blocked", reasons: ["r1"], blocked: [{ profileId: "p", reason: "bad" }] }, 400))
    .toBe("blocked — r1; p: bad");
  // No summary sentence → the HTTP status is preserved (blocked alone does not
  // fabricate a message), matching the existing reasons-only behaviour.
  expect(errorMessage({ detail: { blocked: [{ profileId: "p", slotId: "s", reason: "why" }] } }, 409))
    .toBe("HTTP 409");
});

test("malformed blocked payloads never throw or print [object Object]", () => {
  // blocked as a bare string is a single reason
  expect(errorMessage({ error: "e", blocked: "blocked-elsewhere" }, 400)).toBe("e — blocked-elsewhere");
  // entries that are objects without a scalar reason, or bare non-scalars, are dropped
  expect(errorMessage({ error: "e", blocked: [{ a: 1 }, null, { profileId: "p" }, { reason: { x: 1 } }] }, 400))
    .toBe("e");
  // non-array, non-string blocked values are ignored, never stringified
  expect(errorMessage({ error: "e", blocked: { profileId: "p", reason: "r" } }, 400)).toBe("e — p: r");
  expect(errorMessage({ error: "e", blocked: 42 }, 400)).toBe("e");
  // nested detail.blocked, mixed valid and malformed
  expect(errorMessage({ detail: { error: "e",
    blocked: [null, { profileId: "p", slotId: "s", reason: "r" }, "junk"] } }, 422))
    .toBe("e — p/s: r; junk");
});
