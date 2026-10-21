"""Real-host writer and scope setup regressions; isolated fixture homes only."""
import copy
import threading

import pytest


def test_scope_setup_failure_resets_home(hermes_home, adapter, monkeypatch):
    from agent import secret_scope
    from hermes_constants import get_hermes_home_override

    hermes_home.profile("alpha")
    before = get_hermes_home_override()
    def fail(*args, **kwargs):
        raise RuntimeError("scope setup failed")
    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", fail)
    with pytest.raises(RuntimeError, match="scope setup failed"):
        with adapter.scoped("alpha"):
            pytest.fail("body must not run")
    assert get_hermes_home_override() == before


def test_secret_scope_set_failure_resets_home(hermes_home, adapter, monkeypatch):
    from agent import secret_scope
    from hermes_constants import get_hermes_home_override

    hermes_home.profile("alpha")
    before = get_hermes_home_override()
    def fail(*args, **kwargs):
        raise RuntimeError("scope set failed")
    monkeypatch.setattr(secret_scope, "set_secret_scope", fail)
    with pytest.raises(RuntimeError, match="scope set failed"):
        with adapter.scoped("alpha"):
            pytest.fail("body must not run")
    assert get_hermes_home_override() == before


def test_secret_scope_reset_failure_still_resets_home(hermes_home, adapter, monkeypatch):
    from agent import secret_scope
    from hermes_constants import get_hermes_home_override

    hermes_home.profile("alpha")
    before = get_hermes_home_override()
    reset = secret_scope.reset_secret_scope
    def fail(token):
        reset(token)
        raise RuntimeError("scope reset failed")
    monkeypatch.setattr(secret_scope, "reset_secret_scope", fail)
    with pytest.raises(RuntimeError, match="scope reset failed"):
        with adapter.scoped("alpha"):
            pass
    assert get_hermes_home_override() == before


def _inject_foreign_save(adapter, monkeypatch, timing, change):
    from hermes_cli import config
    save = config.save_config
    def wrapper(document, **kwargs):
        if timing == "after":
            save(document, **kwargs)
        failures = []
        def writer():
            try:
                with adapter.scoped("default"):
                    live = config.require_readable_config_before_write()
                    change(live)
                    save(live)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not failures
        if timing == "before":
            save(document, **kwargs)
    monkeypatch.setattr(config, "save_config", wrapper)


@pytest.mark.parametrize("timing", ["before", "after"])
def test_foreign_write_is_not_adopted(hermes_home, adapter, monkeypatch, timing):
    from hermes_adapter import PostSaveConflict
    from model_switcher.adapters import CanonicalConfigStoreAdapter, StoreWriteAmbiguous

    before = adapter.load_raw_config("default")
    revision = adapter.config_revision("default")
    desired = copy.deepcopy(before)
    desired["model"]["default"] = "our-model"
    def foreign(document):
        document["foreign"] = {"edit": "must survive"}
    _inject_foreign_save(adapter, monkeypatch, timing, foreign)
    with pytest.raises(PostSaveConflict):
        adapter.apply_canonical_save("default", desired, expected_revision=revision)
    after = adapter.load_raw_config("default")
    assert after["foreign"] == {"edit": "must survive"}
    assert after["model"]["default"] == "our-model"
    # The store wrapper must not mislabel this as a pre-write refusal.
    store = CanonicalConfigStoreAdapter(str(hermes_home.path / "config.yaml"),
                                        "default", hermes_adapter=adapter)
    def conflict(*args, **kwargs):
        raise PostSaveConflict("foreign edit")
    monkeypatch.setattr(adapter, "apply_canonical_save", conflict)
    with pytest.raises(StoreWriteAmbiguous) as caught:
        store.write(desired)
    assert caught.value.post_revision is None
    assert getattr(caught.value, "write_phase", None) != "pre_write"


def test_post_save_same_key_write_is_not_adopted(hermes_home, adapter, monkeypatch):
    from hermes_adapter import PostSaveConflict

    before = adapter.load_raw_config("default")
    revision = adapter.config_revision("default")
    desired = copy.deepcopy(before)
    desired["model"]["default"] = "our-model"
    def foreign(document):
        document["model"]["default"] = "their-model"
    _inject_foreign_save(adapter, monkeypatch, "after", foreign)
    with pytest.raises(PostSaveConflict):
        adapter.apply_canonical_save("default", desired, expected_revision=revision)
    assert adapter.load_raw_config("default")["model"]["default"] == "their-model"


@pytest.mark.parametrize("before_extra, after_extra", [
    ({"fallback_model": [{"provider": "openai", "model": "old"}]},
     {"fallback_model": None}),
    ({"fallback_model": [{"provider": "openai", "model": "old"}]},
     {"fallback_model": []}),
    ({"auxiliary": {"vision": {"provider": "openai", "model": "old", "download_timeout": 7}}},
     {"auxiliary": {"vision": {"download_timeout": 7}}}),
    ({"moa": {"presets": {"old": {"aggregator": "old"}, "keep": {"aggregator": "keep"}}}},
     {"moa": {"presets": {"keep": {"aggregator": "keep"}}}}),
    ({}, {"delegation": {"model": None}}),
])
def test_save_preserves_host_sparse_and_removal_semantics(
        hermes_home, adapter, before_extra, after_extra):
    import yaml
    from hermes_cli import config

    model = {"model": {"provider": "openai", "default": "old"}}
    before = dict(copy.deepcopy(model), **copy.deepcopy(before_extra))
    desired = dict(copy.deepcopy(model), **copy.deepcopy(after_extra))
    (hermes_home.path / "config.yaml").write_text(yaml.safe_dump(before))
    # Test-only oracle: runtime private canonicalizer is not used in production.
    expected = config._strip_default_values(
        config._canonicalize_config(desired), config.DEFAULT_CONFIG,
        preserve_keys=set(config._explicit_config_paths(before)))
    adapter.apply_canonical_save("default", desired,
                                 expected_revision=adapter.config_revision("default"))
    assert adapter.load_raw_config("default") == expected


def test_save_only_passes_changed_leaves_to_host(hermes_home, adapter, monkeypatch):
    from hermes_cli import config
    before = adapter.load_raw_config("default")
    revision = adapter.config_revision("default")
    desired = copy.deepcopy(before)
    desired["model"]["default"] = "our-model"
    save = config.save_config
    calls = []
    def wrapper(document, **kwargs):
        calls.append((copy.deepcopy(document), kwargs))
        save(document, **kwargs)
    monkeypatch.setattr(config, "save_config", wrapper)
    result = adapter.apply_canonical_save("default", desired, expected_revision=revision)
    assert result["ok"] is True
    assert calls == [({"model": {"default": "our-model"}}, {"merge_existing": True})]
