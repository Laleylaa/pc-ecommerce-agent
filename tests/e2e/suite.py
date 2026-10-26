"""Local case loading, frozen expectations and deterministic response checks."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VERSION = "chat-e2e-v1"
STATUSES = {"pass", "fail", "unknown", "not_run"}
OPS = {"eq", "nonempty", "empty", "subset", "set_eq", "count", "max", "min"}
COMMON_SEMANTIC = [
    {
        "id": "grounding",
        "expect": "正文中的商品、价格、库存、订单和政策事实符合冻结依据；"
        "未知不说成否定，有限展示不说成全集；外部知识注明实际来源。",
    },
    {
        "id": "delivery",
        "expect": "逐项回应本轮目标，正文、卡片和已展示上下文一致；"
        "不虚报提交、办理或完成，不以工具调用成功代替用户目标达成。",
    },
    {
        "id": "memory",
        "expect": "本套均未请求保存长期偏好；一次性预算、选购和指代条件"
        "不得自动写成长久偏好。结合memory_changes和历史材料核对；当前要求优先。",
    },
]


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def load_cases(suite: str = "all", selected: list[str] | None = None) -> list[dict]:
    cases = []
    for kind in ("business", "boundary"):
        if suite not in ("all", kind):
            continue
        document = read(HERE / kind / "cases.json")
        assert document["version"] == VERSION
        for case in document["cases"]:
            case["suite"] = kind
            for turn in case["turns"]:
                turn["semantic"] = COMMON_SEMANTIC + turn["semantic"]
            cases.append(case)
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ID")
    if selected and set(selected) - set(ids):
        raise ValueError("unknown case ID or case outside selected suite")
    cases = [case for case in cases if not selected or case["id"] in selected]
    if not cases:
        raise ValueError("empty selection")
    for case in cases:
        assert case["requirements"] and case["preconditions"] and case["turns"]
        for turn in case["turns"]:
            assert turn["message"] and turn["semantic"]
            for check in turn["checks"]:
                assert check["op"] in OPS and check["path"] and check["id"]
            for key in ("checks", "semantic"):
                values = [item["id"] for item in turn[key]]
                assert len(values) == len(set(values))
    return cases


def binding_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return {value["$ref"]}
        return set().union(*(binding_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(binding_keys(v) for v in value))
    if isinstance(value, str):
        return set(re.findall(r"\{\{([a-z_0-9]+)\}\}", value))
    return set()


def resolve(value: Any, bindings: dict) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return bindings[value["$ref"]]
        return {key: resolve(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, bindings) for item in value]
    if isinstance(value, str):
        return re.sub(r"\{\{([a-z_0-9]+)\}\}", lambda m: str(bindings[m[1]]), value)
    return value


def extract(value: Any, path: str) -> Any:
    """A missing key is an error, including inside a wildcard; never silently skip it."""
    if not path:
        return value
    head, _, tail = path.partition(".")
    if head == "*":
        if not isinstance(value, list):
            raise TypeError("wildcard requires list")
        return [extract(item, tail) for item in value]
    if isinstance(value, list):
        return extract(value[int(head)], tail)
    return extract(value[head], tail)


def compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "nonempty":
        return isinstance(actual, (str, list, dict)) and bool(
            actual.strip() if isinstance(actual, str) else actual
        )
    if op == "empty":
        return actual in ([], {}, "")
    if op in {"subset", "set_eq"}:
        if not isinstance(actual, list) or not isinstance(expected, list):
            return False
        left = {json.dumps(v, sort_keys=True) for v in actual}
        right = {json.dumps(v, sort_keys=True) for v in expected}
        return left <= right if op == "subset" else left == right
    if op == "count":
        return isinstance(actual, list) and len(actual) == expected
    if op in {"max", "min"}:
        values = actual if isinstance(actual, list) else [actual]
        if not values or any(v is None or isinstance(v, bool) for v in values):
            return False
        return all(
            Decimal(str(v)) <= Decimal(str(expected))
            if op == "max"
            else Decimal(str(v)) >= Decimal(str(expected))
            for v in values
        )
    raise ValueError(f"unknown operator: {op}")


def check_value(body: dict, check: dict) -> dict:
    try:
        actual = extract(body, check["path"])
        passed = compare(actual, check["op"], check.get("expected"))
        return {**check, "actual": actual, "status": "pass" if passed else "fail"}
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation):
        return {**check, "status": "fail", "reason": "missing field or incompatible value"}
