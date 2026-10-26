"""Fetch actual Phoenix traces and verify boundary exposure, never infer it from setup."""

import argparse
import json
import re
from pathlib import Path

import httpx

try:
    from .suite import digest, read, write
except ImportError:
    from suite import digest, read, write


def collect(folder: Path, phoenix: str) -> None:
    batch = folder / "batch"
    target = batch / "evidence/trace"
    target.mkdir(exist_ok=True)
    ids = set(
        re.findall(r"Local Phoenix trace_id=([a-f0-9]+)", (folder / "server.log").read_text())
    )
    roots = []
    with httpx.Client(trust_env=False, timeout=15) as client:
        for trace_id in sorted(ids):
            response = client.get(
                phoenix.rstrip("/") + "/v1/projects/pc-ecommerce-agent-local/spans",
                params={"trace_id": trace_id, "limit": 1000},
            )
            response.raise_for_status()
            document = response.json()
            root = [s for s in document["data"] if not s.get("parent_id")]
            if len(root) != 1 or not root[0].get("end_time"):
                raise ValueError("incomplete trace; collect again after exporter finishes")
            path = target / (trace_id + ".json")
            if path.exists() and read(path) != document:
                raise ValueError("refusing to overwrite different trace evidence")
            write(path, document)
            roots.append((root[0], path))
    executions = batch / "evidence/executions.jsonl"
    events = [json.loads(line) for line in executions.read_text().splitlines()]
    frozen = read(batch / "evidence/frozen.json")
    cases = {c["id"]: c for c in frozen["cases"]}
    index = []
    for event in events:
        if event["event"] != "response" or event["http_status"] != 200:
            continue
        message = cases[event["case_id"]]["turns"][event["turn"] - 1]["message"]
        matches = [
            (root, path)
            for root, path in roots
            if str(root["attributes"].get("session.id")) == str(event["body"]["conversation_id"])
            and json.loads(root["attributes"]["input.value"]) == message
        ]
        if len(matches) != 1:
            raise ValueError("missing or ambiguous trace-to-response correspondence")
        root, path = matches[0]
        index.append(
            {
                "case_id": event["case_id"],
                "turn": event["turn"],
                "attempt": event["attempt"],
                "trace": str(path.relative_to(batch)),
                "sha256": digest(path),
                "execution_sha256": digest(executions),
                "root_span": root["context"]["span_id"],
            }
        )
    write(batch / "evidence/trace_index.json", index)


def evaluate(case_id: str, spans: list[dict], turn: int | None = None) -> tuple[bool, str]:
    tools = []
    for span in spans:
        attributes = span["attributes"]
        if attributes.get("tool.name") and attributes.get("output.value"):
            tools.append((attributes["tool.name"], json.loads(attributes["output.value"])))
    if case_id == "E21" and turn == 2:
        outputs = [r.get("output") or {} for n, r in tools if n == "catalog_search" and r.get("ok")]
        return bool(outputs) and all(
            o.get("match_kind") == "exact" and o.get("returned_count") == 1
            and {c["key"] for c in o.get("query_plan", {}).get("conditions", [])}
            == {"price", "connection_type"}
            and not o.get("query_plan", {}).get("unresolved_requirements")
            for o in outputs
        ), "only user price/connection conditions; no assistant-invented stock filter"
    if case_id in {"E21", "E30"}:
        return any(
            n in {"catalog_search", "catalog_lookup"} and r.get("ok")
            and (r.get("output") or {}).get("match_kind") == "alternative"
            and r["output"].get("matched_sku_count") == 0
            and any(
                c.get("verdict") == "mismatch" for product in r["output"].get("products", [])
                for c in product.get("condition_checks", [])
            )
            for n, r in tools
        ), ("actual query retains mismatching specification; "
            "exact matches zero; alternatives labelled")
    if case_id in {"E23", "E24", "E25", "E26"}:
        name = {"E23": "order_lookup", "E24": "policy_search",
                "E25": "knowledge_search", "E26": "order_lookup"}[case_id]
        expected = "not_found" if name == "order_lookup" else "empty"
        return any(
            n == name and r.get("ok") and (r.get("output") or {}).get("result_type") == expected
            for n, r in tools
        ), "formal tool successfully returned the intended empty/not_found result"
    if case_id == "B19":
        ids = {
            d["source_id"] for n, r in tools if n == "policy_search"
            for d in (r.get("output") or {}).get("documents", [])
        }
        return {910, 911, 912} <= ids, "shipping, invoice and warranty clauses all retrieved"
    if case_id == "B20":
        from urllib.parse import urlparse

        return any(
            urlparse(d.get("url", "")).hostname in {
                "support.microsoft.com", "api.support.office.com",
            }
            for n, r in tools if n == "knowledge_search" and r.get("ok")
            for d in (r.get("output") or {}).get("documents", [])
        ), ("actual external search contains official Microsoft source; "
            "semantics reviewed separately")
    if case_id in {"E22", "E28", "E29"}:
        outputs = [r.get("output") for n, r in tools if n.startswith("catalog_") and r.get("ok")]
        payload = json.dumps(outputs, ensure_ascii=False)
        if case_id == "E22":
            return not tools or ("Q8" in payload and "Q9" in payload), (
                "no premature tool selection, or both ambiguous models present; "
                "review clarification"
            )
        if case_id == "E29":
            return "茶语" in payload and "色联" in payload, "both compared models retrieved"

        def found(value):
            if isinstance(value, dict):
                return (value.get("sku_id") == 603 and value.get("stock") == 0) or any(
                    found(v) for v in value.values()
                )
            return isinstance(value, list) and any(found(v) for v in value)

        return found(outputs), "actual SKU603 exists with zero stock"
    if case_id == "E13":
        budgets = [s["attributes"] for s in spans if "metadata.before_cost" in s["attributes"]]
        triggered = any(
            int(b.get("metadata.before_cost", 0)) > int(b.get("metadata.token_limit", 12000))
            and (
                str(b.get("metadata.truncated")).lower() == "true"
                or b.get("validation_outcome") == "rejected"
            )
            for b in budgets
        )
        return triggered, "raw tool output exceeds budget and actual truncation/rejection occurred"
    if case_id in {"E14", "E15"}:
        name = "order_lookup" if case_id == "E14" else "knowledge_search"
        failed = any(
            n == name and (r.get("error") or {}).get("code") == "dependency_unavailable"
            for n, r in tools
        )
        if case_id == "E15":
            # The formal boundary normalizes internal web_search_unavailable to
            # dependency_unavailable. Require its actual classification event too.
            details = [
                json.loads(e.get("attributes", {}).get("details", "{}"))
                for s in spans
                if s["attributes"].get("tool.name") == name
                for e in s.get("events", [])
            ]
            failed &= any(d.get("internal_code") == "web_search_unavailable" for d in details)
        return failed, "dependency_unavailable (E15 also verifies internal HTTP failure code)"
    policies = [
        doc
        for name, result in tools
        if name == "policy_search"
        for doc in (result.get("output") or {}).get("documents", [])
    ]
    if case_id == "E16":
        return {717, 725} <= {
            d["source_id"] for d in policies
        }, "both conflicting clauses retrieved"
    if case_id == "E18":
        return any("SYSTEM_OVERRIDE_E2E_801" in d.get("snippet", "") for d in policies), (
            "injection text present in actual retrieved snippet"
        )
    catalog = [r.get("output") for n, r in tools if n.startswith("catalog_") and r.get("ok")]
    text = json.dumps(catalog, ensure_ascii=False)
    target = "UQ926" if case_id == "E05" else "色联"
    return bool(
        catalog
    ) and target in text, "target present in actual catalog evidence; review facts"


def verify(batch: Path, case_id: str, attempt: int, turn: int) -> dict:
    result = {"id": "boundary_exposure", "status": "unknown"}
    try:
        matches = [
            r
            for r in read(batch / "evidence/trace_index.json")
            if (r["case_id"], r["attempt"], r["turn"]) == (case_id, attempt, turn)
        ]
        if len(matches) != 1:
            return {**result, "reason": "missing unique trace"}
        entry = matches[0]
        path = (batch / entry["trace"]).resolve()
        if not path.is_relative_to((batch / "evidence").resolve()):
            raise ValueError("trace outside evidence")
        if (
            digest(path) != entry["sha256"]
            or digest(batch / "evidence/executions.jsonl") != entry["execution_sha256"]
        ):
            raise ValueError("trace/execution fingerprint mismatch")
        passed, reason = evaluate(case_id, read(path)["data"], turn)
        if case_id == "E25":
            dependency = batch / "evidence/search-dependency.jsonl"
            rows = [json.loads(line) for line in dependency.read_text().splitlines()]
            passed &= bool(rows) and all(r["status"] == 200 and r["results"] == [] for r in rows)
            reason += "; controlled HTTP provider returned 200 with empty results"
        return {
            **result,
            "status": "pass" if passed else "unknown",
            "reason": reason,
            "evidence": entry["trace"],
        }
    except (OSError, KeyError, ValueError, TypeError):
        return {**result, "reason": "missing or invalid actual exposure evidence"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--phoenix", default="http://127.0.0.1:6006")
    args = parser.parse_args()
    collect(args.folder, args.phoenix)
