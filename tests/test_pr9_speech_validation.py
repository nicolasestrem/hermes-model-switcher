"""Focused RED->GREEN regressions for the PR #9 round-2 speech P2 finding.

Owns: route_inventory.py (the speech-assignment guard) + this file.

Codex P2 #4270985177 — "Reject blank and untrimmed speech route values":

A direct API / saved-preset client could submit a model-bearing speech
assignment such as ``{"provider": "openai", "model": ""}``. The speech branch
checked only ``desc.writable`` (backend has a model key); the generic route
check later verified that the ``model`` key EXISTS, never that its value was a
usable string. The preview then prepared ``tts.provider: openai`` plus an empty
``tts.openai.model`` — a broken explicit route. The same path recognized a
trimmed provider (``" openai "``) but persisted the untrimmed companion value.

Contract enforced here (literal preservation — reject, never canonicalize):

* a model-bearing speech set must carry a ``model`` that is a non-empty string;
* ``provider`` (when present) must be a string, and a non-empty provider must be
  whitespace-clean — ``""`` stays the documented unset/unspecified sentinel;
* neither ``provider`` nor ``model`` may carry leading/trailing whitespace: the
  value refused is refused, and an accepted value persists BYTE-EXACT (never
  silently trimmed to something the client did not send);
* model-only updates (no provider, or ``provider: ""``) stay valid iff the
  runtime-resolved backend is model-bearing; a model-less section (edge/xai/
  piper TTS) still refuses them exactly as before;
* valid explicit provider+model switches and companion writes are unchanged.
"""
from __future__ import annotations

import pytest

import route_inventory as ri

pytest.importorskip("hermes_cli.config_defaults")


def _preview(cfg, payload, slot_id="specialist.tts"):
    return ri.preview_changes(cfg, [ri.RouteEdit(slot_id, payload)])


# ---------------------------------------------------------------------------
# 1 — blank model: an explicit model-bearing switch must not persist ""


@pytest.mark.parametrize("slot_id, cfg", [
    ("specialist.tts", {}),                                    # unset -> edge (model-less)
    ("specialist.stt", {}),                                    # unset -> local (model-bearing)
    ("specialist.tts", {"tts": {"provider": "openai",
                                "openai": {"model": "tts-1"}}}),  # current model-bearing
])
def test_blank_model_assignment_is_refused(slot_id, cfg):
    prev = _preview(cfg, {"provider": "openai", "model": ""}, slot_id)
    assert not prev.clean, prev.changes
    assert prev.changes == (), prev.changes


def test_blank_model_refusal_writes_no_explicit_provider():
    """The refused switch must not leave a half-written explicit backend."""
    prev = _preview({}, {"provider": "openai", "model": ""})
    assert not prev.clean
    assert not any(c.storage_path == "tts.provider" for c in prev.changes)


# ---------------------------------------------------------------------------
# 2 — whitespace-only model (still an unusable id)


@pytest.mark.parametrize("model", ["   ", "\t", "\n"])
def test_whitespace_only_model_is_refused(model):
    prev = _preview({}, {"provider": "openai", "model": model})
    assert not prev.clean, prev.changes
    assert prev.changes == ()


# ---------------------------------------------------------------------------
# 3 — model must be a string (None / number / other)


@pytest.mark.parametrize("model", [None, 123, 4.5, True, ["tts-1"], {"id": "tts-1"}])
def test_non_string_model_is_refused(model):
    prev = _preview({}, {"provider": "openai", "model": model})
    assert not prev.clean, model
    assert prev.changes == (), model


# ---------------------------------------------------------------------------
# 4 — untrimmed model rejected (never silently canonicalized)


@pytest.mark.parametrize("model", [" tts-1", "tts-1 ", " tts-1 ", "\ttts-1"])
def test_untrimmed_model_is_refused(model):
    prev = _preview({}, {"provider": "openai", "model": model})
    assert not prev.clean, model
    assert prev.changes == (), model


# ---------------------------------------------------------------------------
# 5 — untrimmed / malformed provider rejected before backend resolution


@pytest.mark.parametrize("provider", [" openai", "openai ", " openai ", "\topenai"])
def test_untrimmed_provider_is_refused(provider):
    prev = _preview({}, {"provider": provider, "model": "tts-1"})
    assert not prev.clean, provider
    assert prev.changes == (), provider


@pytest.mark.parametrize("provider", [123, None.__class__, ["openai"], {"slug": "openai"}])
def test_non_string_provider_is_refused(provider):
    """A non-string provider never resolves to a usable backend: refused, and
    never stored verbatim."""
    prev = _preview({}, {"provider": provider, "model": "tts-1"})
    assert not prev.clean, provider
    assert prev.changes == (), provider


def test_whitespace_only_provider_is_not_treated_as_unset():
    """``"   "`` is not the documented ``""`` unset sentinel — it is malformed."""
    prev = _preview({}, {"provider": "   ", "model": "tts-1"})
    assert not prev.clean, prev.changes


def test_explicit_invalid_provider_does_not_fall_back_to_current_backend():
    """A present-but-invalid provider must be REFUSED, never allowed to fall
    back to the stored/default model-bearing backend route."""
    cfg = {"tts": {"provider": "openai", "openai": {"model": "tts-1"}}}
    for bad in (None, 123, ["openai"], {"slug": "openai"}):
        prev = _preview(cfg, {"provider": bad, "model": "tts-1-hd"})
        assert not prev.clean, bad
        assert prev.changes == (), bad


# ---------------------------------------------------------------------------
# 6 — accepted identifiers persist BYTE-EXACT (no trim, no rewrite)


def test_accepted_route_persists_byte_exact():
    prev = _preview({}, {"provider": "openai", "model": "tts-1"})
    assert prev.clean, prev.rejected
    paths = {c.storage_path: c.after for c in prev.changes
             if c.storage_path in ("tts.provider", "tts.openai.model")}
    assert paths == {"tts.provider": "openai", "tts.openai.model": "tts-1"}


def test_accepted_model_id_persists_byte_exact_on_model_id_backend():
    prev = _preview({}, {"provider": "elevenlabs", "model": "scribe_v2"})
    assert prev.clean, prev.rejected
    changes = {c.storage_path: c.after for c in prev.changes}
    assert changes["tts.elevenlabs.model_id"] == "scribe_v2"
    assert changes["tts.provider"] == "elevenlabs"


# ---------------------------------------------------------------------------
# 7 — model-only updates preserved on a model-bearing backend


def test_model_only_update_on_model_bearing_backend_is_clean():
    prev = _preview({"tts": {"provider": "openai",
                             "openai": {"model": "tts-1"}}}, {"model": "tts-1-hd"})
    assert prev.clean, prev.rejected
    change = next(c for c in prev.changes if c.storage_path == "tts.openai.model")
    assert change.after == "tts-1-hd"


def test_empty_provider_model_update_resolves_default_backend_unchanged():
    """``provider: ""`` means unset/unspecified: the runtime default (local STT)
    resolves the model-bearing path and NO explicit empty pick is persisted."""
    prev = _preview({}, {"provider": "", "model": "turbo"}, "specialist.stt")
    assert prev.clean, prev.rejected
    change = next(c for c in prev.changes if c.storage_path == "stt.local.model")
    assert change.after == "turbo"
    assert not any(c.storage_path == "stt.provider" and c.after == "" for c in prev.changes)


# ---------------------------------------------------------------------------
# 8 — model-less (voice-only) section still refuses model-only / blank shapes


@pytest.mark.parametrize("payload", [
    {"model": "anything"}, {"model": ""},
    {"provider": "", "model": "x"}, {"provider": "openai", "model": ""},
])
def test_model_less_section_still_refuses_model_only_shapes(payload):
    prev = _preview({}, payload)
    assert not prev.clean, payload
    assert prev.changes == (), payload


# ---------------------------------------------------------------------------
# 9 — the valid explicit switch is unchanged (no over-rejection)


def test_model_less_section_switch_to_supported_backend_unchanged():
    prev = _preview({}, {"provider": "openai", "model": "tts-1"})
    assert prev.clean, prev.rejected
    paths = {c.storage_path: c.after for c in prev.changes
             if c.storage_path in ("tts.provider", "tts.openai.model")}
    assert paths == {"tts.provider": "openai", "tts.openai.model": "tts-1"}


def test_invalid_assignment_is_rejected_before_submitted_backend_lookup(monkeypatch):
    original = ri._speech_descriptor
    submitted_lookups = []

    def observe(cfg, slot_id, provider=None):
        if provider is not None:
            submitted_lookups.append(provider)
        return original(cfg, slot_id, provider)

    monkeypatch.setattr(ri, '_speech_descriptor', observe)
    for payload in ({'provider': ' openai ', 'model': 'tts-1'},
                    {'provider': 'openai', 'model': ' tts-1 '}):
        submitted_lookups.clear()
        prev = _preview({}, payload)
        assert not prev.clean
        assert submitted_lookups == []
