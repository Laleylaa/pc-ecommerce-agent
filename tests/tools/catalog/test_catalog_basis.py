"""Independent expectations read recorded fields, not title semantics."""

import importlib
from pathlib import Path

import pytest


@pytest.mark.contract
@pytest.mark.parametrize("attribute,expected_error", [("否", False), ("是", True)])
def test_recorded_wireless_sources(monkeypatch, attribute, expected_error):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    basis = importlib.import_module("catalog_basis")
    row = {
        "sku_title": "黑色无线版",
        "specs_json": {"wireless": "否", "connection_type": "蓝牙"},
        "attributes": [{"key": "wireless", "value": attribute}],
    }
    if expected_error:
        with pytest.raises(ValueError, match="complete_consistent_wireless"):
            basis.spec(row, "wireless")
    else:
        assert basis.spec(row, "wireless") == "否"
