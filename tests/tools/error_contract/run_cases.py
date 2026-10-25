"""Run only the core offline suite, freezing inputs/source and retaining every batch.

From repository root: backend/.venv/bin/python tests/tools/error_contract/run_cases.py
No service startup, dependency installation, credentials, live models or databases.
Review report.md / semantic.jsonl separately; pytest success is not human acceptance.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
SUITE = Path(__file__).resolve().parent


def main():
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch = ROOT / "artifacts/evals/tool-error-contract" / f"{stamp}-{uuid4().hex[:6]}"
    evidence, checks = batch / "evidence", batch / "checks"
    evidence.mkdir(parents=True)
    checks.mkdir()
    cases = json.loads((SUITE / "cases.json").read_text())
    shutil.copy2(SUITE / "cases.json", evidence / "cases.json")
    sources = [*sorted((ROOT / "backend/app").rglob("*.py")),
               *sorted(SUITE.glob("*.py")), ROOT / "tests/run.py", ROOT / "tests/conftest.py",
               ROOT / "tests/support/offline.py", ROOT / "tests/support/registry_tools.py",
               ROOT / "tests/support/output_budget_fixtures.py", ROOT / "pytest.ini"]
    manifest = {}
    for source in sources:
        relative = source.relative_to(ROOT)
        target = evidence / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
    command = [sys.executable, "tests/run.py", str(SUITE.relative_to(ROOT)),
               "-q", "-o", "junit_family=legacy", f"--junitxml={evidence / 'pytest.xml'}"]
    environment = {
        "started_at": datetime.now(UTC).isoformat(), "command": command,
        "head": subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        "source_sha256": manifest, "environment": cases["environment"],
        "python": sys.version, "model": "none", "database": "none",
    }
    (evidence / "environment.json").write_text(json.dumps(environment, indent=2))
    with (evidence / "pytest.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "ERROR_CONTRACT_EVIDENCE_DIR": str(evidence),
                                     "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
    xml_path = evidence / "pytest.xml"
    tests = ET.parse(xml_path).findall(".//testcase") if xml_path.exists() else []
    indexed = {}
    for test in tests:
        prop = test.find("./properties/property[@name='case_id']")
        if prop is not None:
            indexed[prop.attrib["value"]] = test
    rows = []
    for case in cases["cases"]:
        ident = case["id"]
        test, path = indexed.get(ident), evidence / f"{ident}.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        failure = test is not None and (test.find("failure") is not None
                                        or test.find("error") is not None)
        assertions = record.get("checks", [])
        spans = record.get("spans", [])
        trace_text = json.dumps(spans, ensure_ascii=False)
        trace_checks = [{"check": f"trace_contains:{value}", "comparison": "contains",
                         "expected": value, "actual": value in trace_text,
                         "passed": value in trace_text} for value in case.get("trace_contains", [])]
        all_checks = assertions + trace_checks
        status = ("未执行" if test is None else "失败" if failure or any(
            not c["passed"] for c in all_checks
        ) else "无法判定" if not assertions or not spans or test.find("skipped") is not None
                  else "通过")
        rows.append({"case_id": ident, "status": status, "checks": all_checks,
                     "evidence": f"evidence/{ident}.json", "trace_count": len(spans),
                     "pytest_failure": ET.tostring(test, encoding="unicode") if failure else None})
    (checks / "automatic.jsonl").write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows
    ))
    print((evidence / "pytest.log").read_text())
    print(f"Batch: {batch}")
    print({status: sum(r['status'] == status for r in rows)
           for status in ('通过', '失败', '无法判定', '未执行')})
    return result.returncode or (1 if any(r["status"] != "通过" for r in rows) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
