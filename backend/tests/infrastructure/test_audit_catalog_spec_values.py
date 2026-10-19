
from pathlib import Path

import pytest

from scripts import audit_catalog_spec_values as audit_script


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]


class FakeSession:
    def __init__(self):
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def execute(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return FakeResult(
                [
                    ({"connection_type": "Bluetooth", "color": "Black"}, "鼠标", "Logitech"),
                    ({"connection_type": "蓝牙", "color": "黑色"}, "鼠标", "Logitech"),
                ]
            )
        if self.calls == 2:
            return FakeResult(
                [
                    ("connection_type", "无线", "鼠标", "Razer", 2),
                    ("switches", "红轴", "键盘", "Akko", 3),
                ]
            )
        return FakeResult([(2, 3, 4, 5)])


def test_finalize_stats_sorts_values_by_sku_count() -> None:
    stats = {
        "connection_type": {
            "Wireless": {
                "count": 1,
                "sku_count": 2,
                "categories": {"mouse"},
                "brands": {"Logitech"},
                "sources": {"sku.specs_json"},
            },
            "Bluetooth": {
                "count": 2,
                "sku_count": 5,
                "categories": {"mouse"},
                "brands": {"Razer"},
                "sources": {"attribute_value"},
            },
        }
    }

    finalized = audit_script._finalize_stats(stats, top_values=1)

    assert finalized["connection_type"]["distinct_value_count"] == 2
    assert finalized["connection_type"]["values"][0]["value"] == "Bluetooth"
    assert finalized["connection_type"]["values"][0]["sku_count"] == 5


@pytest.mark.asyncio
async def test_collect_catalog_spec_values_merges_json_and_attribute_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_script, "AsyncSessionLocal", FakeSession)

    audit = await audit_script.collect_catalog_spec_values(top_values=10)

    assert audit["summary"] == {
        "category_count": 2,
        "brand_count": 3,
        "spu_count": 4,
        "sku_count": 5,
        "spec_key_count": 3,
    }
    connection_values = {
        item["value"]: item for item in audit["specs"]["connection_type"]["values"]
    }
    assert connection_values["Bluetooth"]["sources"] == ["sku.specs_json"]
    assert connection_values["无线"]["sources"] == ["attribute_value"]
    assert audit["specs"]["switches"]["values"][0]["sku_count"] == 3


def test_write_markdown_report(tmp_path: Path) -> None:
    output = tmp_path / "audit.md"
    audit = {
        "generated_at": "2026-07-20T00:00:00+00:00",
        "summary": {"sku_count": 2, "spec_key_count": 1},
        "specs": {
            "connection_type": {
                "distinct_value_count": 1,
                "values": [
                    {
                        "value": "Bluetooth",
                        "sku_count": 2,
                        "categories": ["鼠标"],
                        "sources": ["sku.specs_json"],
                    }
                ],
            }
        },
    }

    audit_script.write_markdown_report(audit, output)

    text = output.read_text(encoding="utf-8")
    assert "# Catalog Spec Values Audit" in text
    assert "`connection_type`" in text
    assert "`Bluetooth`" in text
