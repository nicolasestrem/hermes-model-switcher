"""Versioned registry of every user-configurable model-selection slot.

Each descriptor records: slot id, owner/scope, role, storage path, companions,
inheritance/precedence semantics, validator, owning adapter id, activation
boundary, and support state. Unset/default/inherited slots are included —
the registry is built from schema/defaults, not from strings found in YAML.

Code constants (e.g. OMH's shipped model chains) are labeled immutable and
never patched. Historical usage, transcripts and secrets are never slots.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .scanner import EXCLUSIONS as CORE_EXCLUSIONS

REGISTRY_VERSION = 1
# Hermes baseline the descriptors were verified against.
HERMES_BASELINE = "590af66a58c2b3eb3c5edd1dcfefa09c625ae18a (v0.21.7+18.g590af66)"

SUPPORTED = "supported"
GAP = "gap"


@dataclass
class SlotDescriptor:
    slot_id: str
    owner: str                      # "core" | "plugin:<name>"
    role: str                       # main|delegation|auxiliary|judge|reviewer|fallback|moa|automation|specialist|plugin_judge|chain
    storage: str                    # where the value is persisted
    companions: List[str] = field(default_factory=list)
    inheritance: str = "explicit value only"
    validator: str = "non_empty_str_or_empty_means_unset"
    adapter: str = "config_store"
    activation: str = "new sessions"
    support: str = SUPPORTED
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_AUX = "auxiliary.{name}"


def _aux_slot(name: str, *, role: str = "auxiliary", timeout: int = 30,
              note: str = "", effort: bool = True) -> SlotDescriptor:
    companions = ["auxiliary.%s.provider" % name, "auxiliary.%s.base_url" % name,
                  "auxiliary.%s.extra_body" % name]
    if effort:
        companions.append("auxiliary.%s.reasoning_effort" % name)
    return SlotDescriptor(
        slot_id=f"auxiliary.{name}.model",
        owner="core",
        role=role,
        storage=f"config.yaml:{_AUX.format(name=name)}.model",
        companions=companions,
        inheritance='"" (empty) = inherit the main agent model',
        activation="new sessions / next task dispatch",
        notes=note,
    )


CORE_SLOTS: List[SlotDescriptor] = [
    # --- main route ---
    SlotDescriptor(
        slot_id="model",
        owner="core", role="main",
        storage="config.yaml:model",
        companions=["providers", "custom_providers", "fallback_providers[].provider/model"],
        inheritance='"" = first available provider default',
        validator="provider_model_route",
        activation="new sessions; existing chats keep their route",
        notes="Canonical main chat route.",
    ),
    SlotDescriptor(
        slot_id="fallback_providers[].provider/model",
        owner="core", role="fallback",
        storage="config.yaml:fallback_providers[]",
        companions=["fallback.min_switch_reset_seconds"],
        inheritance="order-preserving chain; entry replaces main on rate-limit",
        validator="ordered_chain_entries",
        activation="active turn fallback",
        notes="Preserve order; endpoints are part of route identity.",
    ),
    # --- delegation ---
    SlotDescriptor(
        slot_id="delegation.model",
        owner="core", role="delegation",
        storage="config.yaml:delegation.model",
        companions=["delegation.provider", "delegation.base_url", "delegation.api_mode",
                    "delegation.reasoning_effort"],
        inheritance='"" = inherit parent session model/provider',
        validator="provider_model_route",
        activation="new child sessions",
    ),
    SlotDescriptor(
        slot_id="delegation.fallback_providers[].provider/model",
        owner="core", role="fallback",
        storage="config.yaml:delegation.fallback_providers[]",
        companions=["delegation.model", "delegation.provider"],
        inheritance="null = inherit parent chain; [] = disable fallback for children; "
                    "pinned children get none unless declared here",
        validator="ordered_chain_null_vs_empty",
        activation="new child sessions",
        notes="null vs [] are distinct semantics; never normalize one to the other.",
    ),
    # --- auxiliary tasks ---
    _aux_slot("vision", note="vision analysis route"),
    _aux_slot("compression", timeout=120, note="context compaction"),
    _aux_slot("skills_hub"),
    _aux_slot("approval", role="judge", note="approval classifier; fast/cheap recommended"),
    _aux_slot("review", role="reviewer", note="async /review subagent; api_mode forces transport",
              effort=False),
    _aux_slot("mcp"),
    _aux_slot("title_generation", note="prefer_fast_model opts into provider fast tier"),
    _aux_slot("memory_query_rewrite", effort=False),
    _aux_slot("tts_audio_tags"),
    _aux_slot("voice_chat", timeout=120, note="reasoning_effort=none is the lowest valid tier"),
    _aux_slot("triage_specifier", timeout=120),
    _aux_slot("kanban_decomposer", timeout=180),
    _aux_slot("profile_describer", timeout=60),
    _aux_slot("goal_judge", role="judge", timeout=60,
              note="/goal satisfaction JSON calls; typed decision protocol — chat models not interchangeable"),
    _aux_slot("curator", timeout=600),
    _aux_slot("monitor", timeout=60, note="important-mail scorer; high volume, small model fine"),
    _aux_slot("background_review", timeout=120, note="gated by auxiliary.background_review.enabled"),
    _aux_slot("moa_reference", timeout=900, role="moa", effort=False),
    _aux_slot("moa_aggregator", timeout=900, role="moa", effort=False),
    # --- MoA presets ---
    SlotDescriptor(
        slot_id="moa.presets.<name>.reference_models[].provider/model",
        owner="core", role="moa",
        storage="config.yaml:moa.presets.<name>.reference_models[]",
        companions=["moa.presets.<name>.enabled"],
        inheritance="named presets; list edits bind to route identities, not indexes",
        validator="ordered_chain_entries",
        activation="new sessions using that MoA preset",
        notes="Every named preset, not just default. Reject invalid resulting presets.",
    ),
    SlotDescriptor(
        slot_id="moa.presets.<name>.aggregator.provider/model",
        owner="core", role="moa",
        storage="config.yaml:moa.presets.<name>.aggregator",
        companions=[],
        inheritance="per-preset aggregator route",
        validator="provider_model_route",
        activation="new sessions using that MoA preset",
    ),
    # --- automation ---
    SlotDescriptor(
        slot_id="cron.model",
        owner="core", role="automation",
        storage="config.yaml:cron.model",
        companions=["cron.model_provider"],
        inheritance="per-job pin > cron.model > main model; \"\" falls through",
        validator="provider_model_route",
        activation="next cron run after change",
        notes="cron.model_provider is the INFERENCE provider paired with cron.model; "
              "cron.provider is the scheduler (WHEN) and is protected, never a model slot.",
    ),
    SlotDescriptor(
        slot_id="cron.model_provider",
        owner="core", role="automation",
        storage="config.yaml:cron.model_provider",
        companions=["cron.model"],
        inheritance='"" = resolve from global config',
        validator="provider_id",
        activation="next cron run after change",
        notes="INFERENCE provider paired with cron.model. Distinct from cron.provider "
              "(scheduler). Companion field of the cron.model pair, registered because it "
              "is independently user-configurable.",
    ),
    SlotDescriptor(
        slot_id="cron.jobs[].model",
        owner="core", role="automation",
        storage="cron job store: jobs[].model / jobs[].model_provider",
        companions=["jobs[].model_provider"],
        inheritance="per-job pin > cron.model > main model",
        validator="provider_model_route",
        adapter="cron_store",
        activation="next run of that job",
        notes="Dynamic per-job pins live in the cron store, not config.yaml; "
              "only explicitly selected job fields change.",
    ),
    # --- specialist: speech/voice ---
    SlotDescriptor(
        slot_id="tts.<backend>.model|model_id",
        owner="core", role="specialist",
        storage="config.yaml:tts.<backend>.model (model_id for elevenlabs)",
        companions=["tts.provider", "tts.<backend>.voice_id"],
        inheritance="per-backend literal model ids, not chat routes",
        validator="speech_backend_model",
        activation="next TTS utterance",
        notes="Backends: elevenlabs, openai, gemini, mistral, minimax, kittentts, "
              "neutts, deepinfra. Capability-filtered; never map a chat model here.",
    ),
    SlotDescriptor(
        slot_id="stt.<backend>.model|model_id",
        owner="core", role="specialist",
        storage="config.yaml:stt.<backend>.model (model_id for elevenlabs)",
        companions=["stt.provider"],
        inheritance="per-backend literal model ids (whisper/voxtral/scribe families)",
        validator="speech_backend_model",
        activation="next STT request",
        notes="Backends: local, groq, openai, mistral, xai, elevenlabs, deepinfra.",
    ),
    SlotDescriptor(
        slot_id="voice.gpt_live.model",
        owner="core", role="specialist",
        storage="config.yaml:voice.gpt_live.model",
        companions=["voice.gpt_live.provider"],
        inheritance="literal realtime-model id",
        validator="speech_backend_model",
        activation="next realtime voice session",
    ),
    SlotDescriptor(
        slot_id="x_search.model",
        owner="core", role="specialist",
        storage="config.yaml:x_search.model",
        companions=["x_search.provider"],
        inheritance="literal Grok search-model id",
        validator="literal_model_id",
        activation="next x_search call",
    ),
]

# OMH ships its delegation model chains as code constants plus an override
# file outside config.yaml. Constants are immutable; only the routing file
# is a user-editable store (via its own writer, `omh model-chains set`).
OMH_CHAINS_SLOT = SlotDescriptor(
    slot_id="omh.routing.model-chains.categories.<name>.chain[]",
    owner="plugin:omh",
    role="chain",
    storage="~/.omh/routing/model-chains.json: categories.<name>.chain[] entries {model, reasoning_effort}",
    companions=["reasoning_effort per entry"],
    inheritance="shipped code constants are the fallback; the routing file overrides per category",
    validator="chain_entry_token",
    adapter="omh_chains_store",
    activation="new delegated child sessions labeled by that category",
    notes="Code constants (hermes_delegation.py) are immutable — labeled, never patched. "
          "Entries must be plain identifier tokens; unknown keys rejected.",
)

JUDGE_PLUGIN_MODELS = ("jev_model", "typesafe_model", "openrouter_model")

JEV_JUDGE_SLOTS: List[SlotDescriptor] = [
    SlotDescriptor(
        slot_id=f"plugin:jev-judge:config_schema.{name}",
        owner="plugin:jev-judge",
        role="plugin_judge",
        storage=f"config.yaml:plugins.entries.jev-judge.settings.{name}",
        companions=[f"plugins.entries.jev-judge.settings.{name.replace('model', 'base_url')}",
                    "plugins.entries.jev-judge.settings.backend"],
        inheritance="backend selects which model field is live (auto = typesafe, else openrouter, else gateway)",
        validator="literal_model_id",
        adapter="plugin_settings_store",
        activation="next judged tool call",
        notes="Thresholds (destructive/exfiltration/impact/...) are protected — never changed "
              "to make an assignment pass.",
    )
    for name in JUDGE_PLUGIN_MODELS
]

PLUGIN_SLOTS: List[SlotDescriptor] = JEV_JUDGE_SLOTS + [OMH_CHAINS_SLOT]

# Plugin config-schema candidates discovered by scanner but NOT model slots.
PLUGIN_EXCLUSIONS: Dict[str, str] = {}

# Installed plugins with model-selection config_schema fields. Discovery is
# by manifest scan; this table gives each a role. A plugin here with no
# registry entry for a scanned field is a coverage gap (never auto-allowed).
PLUGIN_ROLES: Dict[str, str] = {
    "jev-judge": "plugin_judge",      # pre-tool judgment layer
    "jev-skill-router": "plugin_router",  # pre-model skill selection judge
}


def _plugin_slots() -> List[SlotDescriptor]:
    slots = list(JEV_JUDGE_SLOTS)
    for name in ("jev-skill-router",):
        role = PLUGIN_ROLES[name]
        for field_name in ("jev_model", "typesafe_model", "openrouter_model"):
            slots.append(SlotDescriptor(
                slot_id=f"plugin:{name}:config_schema.{field_name}",
                owner=f"plugin:{name}",
                role=role,
                storage=f"config.yaml:plugins.entries.{name}.settings.{field_name}",
                companions=[f"plugins.entries.{name}.settings.backend",
                            f"plugins.entries.{name}.settings."
                            + field_name.replace("model", "base_url")],
                inheritance="backend selects which model field is live",
                validator="literal_model_id",
                adapter="plugin_settings_store",
                activation="next routed/selected call",
                notes="Thresholds and routing gates are protected — never changed to "
                      "make an assignment pass.",
            ))
    return slots


PLUGIN_SLOTS: List[SlotDescriptor] = _plugin_slots() + [OMH_CHAINS_SLOT]


def all_slots() -> List[SlotDescriptor]:
    return CORE_SLOTS + PLUGIN_SLOTS


def build_registry() -> Dict[str, Any]:
    exclusions = dict(sorted({**CORE_EXCLUSIONS, **PLUGIN_EXCLUSIONS}.items()))
    return {
        "registry_version": REGISTRY_VERSION,
        "hermes_baseline": HERMES_BASELINE,
        "slots": [s.to_dict() for s in all_slots()],
        "exclusions": exclusions,
    }


def slot_index() -> Dict[str, SlotDescriptor]:
    return {s.slot_id: s for s in all_slots()}
