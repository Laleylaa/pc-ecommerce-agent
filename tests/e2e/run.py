"""Explicit HTTP E2E runner: template -> prepare -> live. No automatic service setup."""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

try:
    from .suite import (
        ROOT,
        VERSION,
        binding_keys,
        digest,
        load_cases,
        now,
        read,
        resolve,
        write,
    )
except ImportError:  # Direct CLI entry, without importing the application or reading .env.
    from suite import (
        ROOT,
        VERSION,
        binding_keys,
        digest,
        load_cases,
        now,
        read,
        resolve,
        write,
    )


def selected_cases(args: argparse.Namespace) -> list[dict]:
    cases = load_cases(args.suite, args.case)
    selected = [case for case in cases if case["profile"] == args.profile]
    if not selected or (args.case and len(selected) != len(cases)):
        raise ValueError("select cases matching --profile; fault profiles run separately")
    return selected


def template(cases: list[dict], profile: str) -> dict:
    return {
        "version": VERSION,
        "target": {
            "base_url": "http://127.0.0.1:8000/api",
            "profile": profile,
            "token_env": "E2E_TOKEN",
            "user_id": None,
            "runtime": "strands",
            "model": None,
            "server_revision": None,
            "dataset_revision": None,
            "valid_until": None,
            "synthetic_account_only": False,
            "initial_preferences_verified": False,
        },
        "cases": {
            case["id"]: {
                "ready": False,
                "verified_at": None,
                "verified_by": None,
                "facts": None,
                "evidence_files": [],
                "bindings": dict.fromkeys(sorted(binding_keys(case))),
                "preconditions_to_verify": case["preconditions"],
            }
            for case in cases
        },
    }


def validate_target(target: dict) -> None:
    url = urlsplit(target["base_url"])
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("target must be a credential-free HTTP(S) API URL")
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("remote targets require HTTPS")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", target["token_env"]):
        raise ValueError("token_env must name an environment variable, not contain a token")
    if type(target["user_id"]) is not int or target["user_id"] <= 0:
        raise ValueError("independently verified test user ID required")
    if target["runtime"] != "strands":
        raise ValueError("this suite targets the strands runtime")
    for field in ("model", "server_revision", "dataset_revision"):
        if not isinstance(target[field], str) or not target[field].strip():
            raise ValueError(f"missing target metadata: {field}")
    for field in ("synthetic_account_only", "initial_preferences_verified"):
        if target[field] is not True:
            raise ValueError(f"target precondition not verified: {field}")
    if datetime.fromisoformat(target["valid_until"]) <= datetime.fromisoformat(now()):
        raise ValueError("baseline expired; independently reverify in a new batch")


def source_paths() -> list[Path]:
    result = []
    for directory in (ROOT / "backend/app", ROOT / "tests/e2e"):
        result.extend(
            p
            for p in directory.rglob("*")
            if p.suffix in {".py", ".md", ".json"} and "__pycache__" not in p.parts
        )
    result.extend(
        ROOT / name
        for name in (
            "backend/uv.lock",
            "backend/pyproject.toml",
            "tests/README.md",
            "tests/REVIEW.md",
        )
    )
    return sorted(result)


def prepare(cases: list[dict], oracle_path: Path, batch: Path, repeat: int, timeout: int) -> None:
    oracle = read(oracle_path)
    if oracle["version"] != VERSION or not 1 <= repeat <= 3 or not 240 <= timeout <= 600:
        raise ValueError("invalid version/repeat/timeout")
    validate_target(oracle["target"])
    frozen = []
    files = []
    for case in cases:
        data = oracle["cases"][case["id"]]
        if case["profile"] != oracle["target"]["profile"]:
            raise ValueError("case requires a different server profile")
        if data["ready"] is not True or not data["facts"] or not data["verified_by"]:
            raise ValueError(f"{case['id']}: independent facts and reviewer required")
        if datetime.fromisoformat(data["verified_at"]) > datetime.fromisoformat(now()):
            raise ValueError("verification date is in the future")
        keys = binding_keys(case)
        if any(data["bindings"].get(key) is None for key in keys):
            raise ValueError(f"{case['id']}: unresolved bindings")
        if not data["evidence_files"]:
            raise ValueError("independent baseline evidence files required")
        copied = []
        for index, filename in enumerate(data["evidence_files"]):
            source = (oracle_path.parent / filename).resolve()
            if not source.is_file() or source.name.startswith(".env"):
                raise ValueError("baseline evidence must be an explicit non-secret file")
            destination = f"evidence/baseline/{case['id']}-{index}{source.suffix}"
            files.append((source, destination))
            copied.append(destination)
        frozen.append(
            {
                **resolve(case, data["bindings"]),
                "oracle": {
                    **data,
                    "evidence_files": copied,
                },
            }
        )
    # Fail before writing anything if input is incomplete. Never reuse an old batch.
    batch.mkdir(parents=True, exist_ok=False)
    for source, destination in files:
        (batch / destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, batch / destination)
    for source in source_paths():
        destination = batch / "evidence/source" / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    write(
        batch / "evidence/frozen.json",
        {
            "version": VERSION,
            "prepared_at": now(),
            "head": head,
            "target": oracle["target"],
            "repeat": repeat,
            "timeout": timeout,
            "cases": frozen,
            "excluded_case_ids": sorted({c["id"] for c in load_cases()} - {c["id"] for c in cases}),
            "metadata_note": (
                "server/model/data metadata supplied by operator, not inferred from API"
            ),
        },
    )
    write(
        batch / "manifest.json",
        {
            str(p.relative_to(batch)): digest(p)
            for p in sorted((batch / "evidence").rglob("*"))
            if p.is_file()
        },
    )


def verify_batch(batch: Path, *, live: bool = False) -> dict:
    for filename, expected in read(batch / "manifest.json").items():
        if digest(batch / filename) != expected:
            raise ValueError(f"frozen evidence changed: {filename}")
        if live and filename.startswith("evidence/source/"):
            local = ROOT / filename.removeprefix("evidence/source/")
            if not local.is_file() or digest(local) != expected:
                raise ValueError(f"local source changed since prepare: {local}")
    frozen = read(batch / "evidence/frozen.json")
    if frozen["version"] != VERSION:
        raise ValueError("unsupported frozen version")
    return frozen


def append(batch: Path, row: dict) -> None:
    with (batch / "evidence/executions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), **row}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def execute(batch: Path, frozen: dict, client: httpx.Client) -> None:
    """No retries: an HTTP timeout may already have committed a chat turn."""
    for case in frozen["cases"]:
        for attempt in range(1, frozen["repeat"] + 1):
            conversation_id = None
            for number, turn in enumerate(case["turns"], 1):
                key = {"case_id": case["id"], "attempt": attempt, "turn": number}
                payload = {"message": turn["message"]}
                if conversation_id is not None:
                    payload["conversation_id"] = conversation_id
                append(batch, {**key, "event": "started", "request": payload})
                start = time.monotonic()
                try:
                    response = client.post("chat", json=payload)
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"non_json_body": response.text}
                    append(
                        batch,
                        {
                            **key,
                            "event": "response",
                            "http_status": response.status_code,
                            "body": body,
                            "seconds": round(time.monotonic() - start, 3),
                        },
                    )
                except httpx.HTTPError as exc:
                    # Do not store exception strings containing endpoint or credential material.
                    append(batch, {**key, "event": "transport_error", "error": type(exc).__name__})
                    break
                if response.status_code != 200 or not isinstance(body, dict):
                    break
                received_id = body.get("conversation_id")
                if type(received_id) is not int or received_id <= 0:
                    break
                if conversation_id is not None and received_id != conversation_id:
                    break
                conversation_id = received_id
            if conversation_id is not None:
                key = {"case_id": case["id"], "attempt": attempt}
                try:
                    response = client.get(f"conversations/{conversation_id}")
                    append(
                        batch,
                        {
                            **key,
                            "event": "history",
                            "http_status": response.status_code,
                            "body": response.json(),
                        },
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    append(batch, {**key, "event": "history_error", "error": type(exc).__name__})


def live(batch: Path) -> None:
    frozen = verify_batch(batch, live=True)
    target = frozen["target"]
    validate_target(target)
    token = os.environ.get(target["token_env"])
    if not token:
        raise ValueError(
            "set the designated bearer-token environment variable; never put it in JSON"
        )
    # Exclusive marker prevents resume/replay after timeout or crash.
    with (batch / "evidence/run_started.json").open("x", encoding="utf-8") as stream:
        json.dump({"at": now()}, stream)
    with httpx.Client(
        base_url=target["base_url"].rstrip("/") + "/",
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        follow_redirects=False,
        timeout=frozen["timeout"],
    ) as client:
        try:
            identity = client.get("auth/me")
            if identity.status_code != 200 or identity.json().get("id") != target["user_id"]:
                raise ValueError("test account identity mismatch; no chat sent")
            append(batch, {"event": "identity_verified", "user_id": target["user_id"]})
            execute(batch, frozen, client)
        finally:
            executions = batch / "evidence/executions.jsonl"
            write(
                batch / "evidence/run_finished.json",
                {
                    "at": now(),
                    "executions_sha256": digest(executions) if executions.exists() else None,
                },
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("template", "prepare"):
        item = sub.add_parser(command)
        item.add_argument("--suite", choices=("all", "business", "boundary"), default="all")
        item.add_argument("--case", action="append")
        item.add_argument("--profile", default="normal")
        item.add_argument("--output", type=Path, required=True)
        if command == "prepare":
            item.add_argument("--oracle", type=Path, required=True)
            item.add_argument("--repeat", type=int, default=1)
            item.add_argument("--timeout", type=int, default=300)
    item = sub.add_parser("live")
    item.add_argument("--batch", type=Path, required=True)
    item.add_argument(
        "--confirm-live-writes",
        action="store_true",
        required=True,
        help="acknowledge model cost and chat/memory/audit database writes",
    )
    args = parser.parse_args()
    if args.command == "template":
        if args.output.exists():
            raise ValueError("refusing to overwrite oracle")
        write(args.output, template(selected_cases(args), args.profile))
    elif args.command == "prepare":
        prepare(selected_cases(args), args.oracle, args.output, args.repeat, args.timeout)
    else:
        live(args.batch)


if __name__ == "__main__":
    main()
