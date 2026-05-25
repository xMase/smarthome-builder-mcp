import pytest

from nrha_mcp.deploy import diff_flows, commit


def test_diff_detects_add_remove_change():
    before = [{"id": "a", "name": "x"}, {"id": "b"}]
    after = [{"id": "a", "name": "y"}, {"id": "c"}]
    d = diff_flows(before, after)
    assert d["added"] == ["c"]
    assert d["removed"] == ["b"]
    assert d["changed"] == {"a": {"name": {"before": "x", "after": "y"}}}
