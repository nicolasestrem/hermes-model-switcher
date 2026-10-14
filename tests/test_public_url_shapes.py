"""Public URL projection must not serialize malformed secret-bearing containers."""
import json

import pytest

from dashboard.plugin_api import _public_diff_value, _sanitize_public_url


@pytest.mark.parametrize("malformed", [
    {"api_key": "SYNTHETIC-PRIVATE-CANARY"},
    [{"Authorization": "SYNTHETIC-PRIVATE-CANARY"}],
])
def test_malformed_base_url_does_not_echo_nested_payload(malformed):
    direct = _sanitize_public_url(malformed)
    projected = _public_diff_value({"base_url": malformed}, "model")
    assert direct == "<unparseable URL>"
    assert projected["base_url"] == "<unparseable URL>"
    assert "SYNTHETIC-PRIVATE-CANARY" not in json.dumps(projected)


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_base_url_preserves_clear_vs_set_semantics(empty):
    assert _sanitize_public_url(empty) == empty
