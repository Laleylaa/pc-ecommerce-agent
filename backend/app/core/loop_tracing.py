"""Local-only P4 spans. Private provider; no global HTTP/SQL/SDK instrumentation."""

import asyncio
import atexit
import json
import logging
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from threading import Lock, Thread

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from app.core.loop_budget import current_model_stage

log = logging.getLogger(__name__)
ENDPOINT = "http://127.0.0.1:6006/v1/traces"
LIMIT = 32768
_provider = None
_lock = Lock()
_scope = ContextVar("local_trace_scope", default=None)
_secrets = ContextVar("local_trace_secrets", default=())
_last_model = ContextVar("local_trace_last_model", default=None)
_SENSITIVE = re.compile(
    r"password|passwd|secret|api[_-]?key|authorization|cookie|access_token|refresh_token|"
    r"connection_string|database_url|email|phone|address|recipient|customer_name|"
    r"receiver_name|contact_name|姓名|电话|邮箱|住址|收货", re.I,
)


def clean(value: object, depth: int = 0) -> object:
    """Snapshot JSON-compatible data only; never serialize arbitrary clients/settings."""
    if depth > 16:
        return "[已截断: nesting limit]"
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SENSITIVE.search(str(k)) else clean(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value]
    if isinstance(value, str):
        # Model messages and function arguments often contain JSON encoded inside JSON.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
                sanitized = clean(parsed, depth + 1)
                return value if parsed == sanitized else json.dumps(sanitized, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for secret in _secrets.get():
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED email]", value)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[REDACTED phone]", value)
        value = re.sub(r"(\w+://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", value)
        credential = r"(?:api[_-]?key|password|passwd|secret|access_token|refresh_token|token)"
        value = re.sub(
            r"(?i)([\"']?" + credential + r"[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
            r"\1[REDACTED]", value,
        )
        value = re.sub(
            r"(?i)((?:" + credential + r")[\"']?\s*[:=]\s*)[^\s,;\"']+",
            r"\1[REDACTED]", value,
        )
        return value
    if value is None or type(value) in (bool, int, float):
        return value
    return "[OMITTED: unsupported object]"


def snapshot(value: object) -> str:
    text = json.dumps(clean(value), ensure_ascii=False, allow_nan=False)
    if len(text) > LIMIT:
        return json.dumps({"truncated": True, "notice": "已截断；资料不完整，无附件",
                           "original_chars": len(text), "preview": text[:LIMIT]},
                          ensure_ascii=False)
    return text


def fields(**values: object) -> None:
    span = _scope.get()
    if span is None:
        return
    try:
        for key, value in values.items():
            if value is not None:
                safe = clean(value)
                span.set_attribute(key, safe if type(safe) in (bool, int, float)
                                   or isinstance(safe, str) and len(safe) <= LIMIT
                                   else snapshot(safe))
    except Exception:
        log.warning("Trace fields lost; business continues", exc_info=False)


def metadata(**values: object) -> None:
    """A small UI overview; never replaces exported messages, results or events."""
    span = _scope.get()
    if span is None:
        return
    try:
        current = json.loads(span.attributes.get("metadata", "{}"))
        current.update(clean(values))
        text = json.dumps(current, ensure_ascii=False, allow_nan=False)
        if len(text) > 4096:
            text = json.dumps({"summary_truncated": True,
                               "notice": "摘要已截断；完整已采集字段见Messages/Attributes/Events",
                               "preview": text[:3800]}, ensure_ascii=False)
        span.set_attribute("metadata", text)
    except Exception:
        log.warning("Trace summary lost; business continues", exc_info=False)


def _pick(value: object, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value}


def _object(value: object) -> dict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _message_object(message: dict) -> dict:
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict) and p.get("type") == "text")
    return _object(content)


def model_input_summary(body: dict, stage: str, attempt: int) -> None:
    user = next((m for m in body.get("messages", []) if m.get("role") == "user"), {})
    data = _message_object(user)
    source = data.get("original_request", data)
    summary = _pick(source, ("query", "message", "limit", "offset", "category_hint"))
    for key in ("query", "message"):
        if isinstance(summary.get(key), str) and len(summary[key]) > 240:
            summary[key] = summary[key][:240] + "…[摘要省略]"
    if stage == "answer":
        summary = {"goal_count": len(data.get("goals", [])),
                   "evidence_count": len(data.get("evidence", [])),
                   "projected_evidence_count": sum(bool(e.get("projection_truncated"))
                                                   for e in data.get("evidence", []))}
    if stage == "tool_review":
        summary["candidate_plan"] = _pick(
            data.get("candidate_plan"),
            ("operation", "search_text", "category", "sort", "requested_limit"),
        )
    metadata(purpose=PURPOSES.get(stage, "模型调用"), request_index=attempt,
             input_overview=summary, detail_location="Messages / Attributes / Events")


def model_output_summary(messages: list, categories: list[str], stage: str) -> None:
    actions = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name")
            args = _object(function.get("arguments"))
            item = {"action": name}
            if name == "register_goals":
                goals = _object(args.get("registration")).get("goals", [])
                item["goal_ids"] = [g.get("id") for g in goals[:8]]
            elif name == "query_complete":
                item["reason"] = _object(args.get("completion")).get("reason")
            else:
                item.update(_pick(args, ("goal_id",)))
                item["arguments"] = _pick(args.get("arguments"),
                                          ("query", "category_hint", "limit", "offset"))
            actions.append(item)
    data = _message_object(messages[0]) if messages else {}
    summary = _pick(
        data, ("operation", "search_text", "category", "sort", "requested_limit", "verdict"),
    )
    if stage == "tool_planning":
        summary["condition_count"] = len(data.get("conditions", []))
    elif stage == "tool_review":
        if isinstance(data.get("reason"), str):
            summary["reason_preview"] = data["reason"][:240]
            summary["preview_truncated"] = len(data["reason"]) > 240
    elif stage == "answer":
        summary.update(_pick(data, ("covered_goal_ids", "evidence_ids")))
        if isinstance(data.get("response"), str):
            summary["answer_preview"] = data["response"][:240]
            summary["preview_truncated"] = len(data["response"]) > 240
    metadata(output_categories=categories, submitted_actions=actions[:8],
             actions_omitted=max(0, len(actions) - 8), output_overview=summary,
             interpretation="模型提交内容；是否接受请看程序事件及实际工具结果")


def payload(kind: str, value: object) -> None:
    span = _scope.get()
    if span is not None:
        try:
            span.set_attribute(f"{kind}.value", snapshot(value))
            span.set_attribute(f"{kind}.mime_type", "application/json")
            if span.attributes.get("openinference.span.kind") == "TOOL":
                if kind == "input":
                    metadata(tool_input=_pick(value, ("query", "category_hint", "limit", "offset")))
                elif isinstance(value, dict):
                    output = value.get("output") or {}
                    metadata(tool_result={"ok": value.get("ok"),
                             **_pick(output, ("status", "reason_code", "match_kind",
                                              "returned_count", "total_match_count",
                                              "requested_count", "limit", "limit_capped",
                                              "is_exhaustive", "selection_scope")),
                             "error": _pick(value.get("error"), ("code", "retryable"))})
        except Exception:
            log.warning("Trace payload lost; business continues", exc_info=False)


def rejection(raw: object, error: object, feedback: object = None) -> None:
    span = _scope.get()
    if span is not None:
        try:
            span.add_event("validation.rejected", {
                "submission": snapshot(raw), "error": snapshot(error),
                "feedback": snapshot(feedback),
                "model_span_id": (_last_model.get() or {}).get("id", "unknown"),
            })
        except Exception:
            log.warning("Trace rejection lost; business continues", exc_info=False)


def program_event(name: str, *, outcome: str, related_model: bool = False, **data) -> None:
    """Small program decisions on the owning tool/request, never new business work."""
    span = _scope.get()
    if span is None:
        return
    try:
        attrs = {"record_type": "program_decision", "outcome": outcome,
                 "details": snapshot(data)}
        if related_model:
            attrs["model_span_id"] = (_last_model.get() or {}).get("id", "unknown")
        span.add_event(f"程序 · {name}", attrs)
        if name == "材料投影":
            metadata(last_material_projection={"outcome": outcome,
                     **_pick(data, ("destination", "original_chars", "projected_chars",
                                    "limit_chars", "source_product_count",
                                    "retained_product_count"))})
        elif name == "排序与返回窗口":
            metadata(result_window=_pick(data, ("sort", "selection_scope", "total_count",
                                                "offset", "requested_count", "limit",
                                                "limit_capped", "returned_count")))
        elif outcome == "rejected":
            metadata(last_validation_rejection={"event": name,
                     **_pick(data, ("phase", "attempt", "feedback")),
                     "model_span_id": attrs.get("model_span_id", "unknown")})
        elif name == "计划复核结论":
            metadata(review_result={"verdict": outcome,
                     **_pick(data, ("attempt", "corrections", "next_action")),
                     "reason_preview": str(data.get("reason", ""))[:240],
                     "reason_truncated": len(str(data.get("reason", ""))) > 240})
    except Exception:
        log.warning("Trace program event lost; business continues", exc_info=False)


def tracing_active() -> bool:
    return _scope.get() is not None


class SafeExporter(SpanExporter):
    def __init__(self, exporter):
        self.exporter = exporter

    def export(self, spans):
        try:
            result = self.exporter.export(spans)
            if result == SpanExportResult.SUCCESS:
                return result
        except Exception:
            pass
        log.warning("Phoenix export failed: %d spans lost; trace may be incomplete", len(spans))
        return SpanExportResult.FAILURE

    def shutdown(self):
        self.exporter.shutdown()


def local_exporter() -> SafeExporter:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from requests import Session

    class LocalSession(Session):
        def send(self, request, **kwargs):
            if request.url != ENDPOINT:
                raise ValueError("local_trace_endpoint_required")
            # Ignore ambient OTEL headers, proxy/netrc configuration and redirects.
            request.headers = {"Content-Type": "application/x-protobuf"}
            kwargs.update(allow_redirects=False, proxies={})
            return super().send(request, **kwargs)

    session = LocalSession()
    session.trust_env = False
    return SafeExporter(OTLPSpanExporter(
        endpoint=ENDPOINT, timeout=1, headers={}, session=session,
        compression=Compression.NoCompression,
    ))


def provider() -> TracerProvider:
    global _provider
    with _lock:
        if _provider is None:
            result = TracerProvider(
                resource=Resource({"service.name": "pc-ecommerce-agent",
                                   "openinference.project.name": "pc-ecommerce-agent-local"}),
                span_limits=SpanLimits(
                    max_attributes=512, max_span_attributes=512, max_events=128,
                    max_event_attributes=8, max_links=0, max_link_attributes=0,
                    max_attribute_length=262144, max_span_attribute_length=262144,
                ),
                shutdown_on_exit=False,
            )
            result.add_span_processor(BatchSpanProcessor(
                local_exporter(), max_queue_size=256, max_export_batch_size=16,
                schedule_delay_millis=500, export_timeout_millis=1500,
            ))
            _provider = result
        return _provider


def shutdown() -> None:
    global _provider
    with _lock:
        instance, _provider = _provider, None
    if instance is not None:
        worker = Thread(target=instance.shutdown, daemon=True)
        worker.start()
        worker.join(2)
        if worker.is_alive():
            log.warning("Phoenix shutdown timed out; pending traces may be lost")


atexit.register(shutdown)


@contextmanager
def step(name, kind="CHAIN", *, root=False):
    parent = _scope.get()
    span = None
    token = None
    try:
        if root or parent is not None:
            context = (trace.set_span_in_context(parent)
                       if parent is not None else otel_context.Context())
            span = provider().get_tracer("pc.loop").start_span(name, context=context)
            span.set_attribute("openinference.span.kind", kind)
            token = _scope.set(span)
    except Exception:
        log.warning("Trace initialization failed; business continues", exc_info=False)
    try:
        yield span
    except BaseException as exc:
        if span is not None:
            fields(outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                   error_type=type(exc).__name__, stop_reason=type(exc).__name__)
            span.set_status(trace.StatusCode.ERROR, type(exc).__name__)
        raise
    finally:
        if span is not None:
            try:
                span.end()
            except Exception:
                log.warning("Trace end failed; record may be lost", exc_info=False)
        if token is not None:
            _scope.reset(token)


@contextmanager
def validation_step(name: str, *, related_model: bool = False, **context):
    """Time an existing validation only; preserve its return/exception behavior."""
    model_id = (_last_model.get() or {}).get("id", "unknown") if related_model else None
    with step(f"程序 · {name}") as span:
        fields(record_type="program_validation", validation_name=name,
               model_span_id=model_id, **context)
        metadata(purpose=name, model_span_id=model_id, **context)
        try:
            yield
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "rejected"
            fields(validation_outcome=outcome)
            metadata(validation_outcome=outcome, error_type=type(exc).__name__, error=str(exc))
            payload("output", {"validation_outcome": outcome, "error": str(exc)})
            if span is not None:
                try:
                    label = "取消" if outcome == "cancelled" else "拒绝"
                    span.update_name(f"程序 · {name}【{label}】")
                except Exception:
                    log.warning("Trace validation label lost; business continues")
            raise
        else:
            fields(validation_outcome="accepted")
            metadata(validation_outcome="accepted")
            payload("output", {"validation_outcome": "accepted"})
            if span is not None:
                try:
                    span.update_name(f"程序 · {name}【通过】")
                    span.set_status(trace.StatusCode.OK)
                except Exception:
                    log.warning("Trace validation label lost; business continues")


def code_version() -> dict[str, str | bool]:
    root = Path(__file__).resolve().parents[3]
    try:
        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=root, timeout=0.5, stderr=subprocess.DEVNULL,
            ).decode().strip()
        return {"revision": git("rev-parse", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=normal"))}
    except Exception:
        return {"revision": "unknown", "dirty": "unknown"}


def traced_turn(fn):
    @wraps(fn)
    async def run(self, request, user_id):
        if not self.settings.loop_trace_enabled:
            return await fn(self, request, user_id)
        secrets = tuple(v for v in (
            self.settings.llm_api_key, self.settings.auth_token_secret,
            self.settings.deepseek_search_api_key.get_secret_value(),
            self.settings.langsmith_api_key.get_secret_value(),
        ) if v)
        secret_token = _secrets.set(secrets)
        last_token = _last_model.set({})
        # Never inherit another request's scope, including tasks spawned inside a request.
        scope_token = _scope.set(None)
        try:
            with step("用户请求", root=True) as span:
                fields(runtime="strands", model=self.settings.llm_model, **code_version())
                payload("input", request.message)
                if span is not None:
                    trace_id = format(span.context.trace_id, "032x")
                    log.warning("Local Phoenix trace_id=%s http://127.0.0.1:6006", trace_id)
                response = await fn(self, request, user_id)
                payload("output", response.answer)
                fields(outcome="succeeded", **{"session.id": str(response.conversation_id)})
                return response
        finally:
            _scope.reset(scope_token)
            _last_model.reset(last_token)
            _secrets.reset(secret_token)
    return run


# Bounded flattened attributes: two directions use at most 180 each, leaving room
# for span metadata under the 512 attribute SDK limit. No duplicate raw message dump.
MODEL_ATTRIBUTE_LIMIT = 180
PURPOSES = {
    "query": "任务规划与调度", "tool_planning": "工具查询规划",
    "tool_review": "工具计划复核", "answer": "回答生成",
}
OUTPUT_LABELS = {
    "goal_registration": "目标登记提案", "business_tool_call": "业务工具请求",
    "query_completion": "查询结束申请", "other_tool_call": "其他工具请求",
    "query_text": "查询阶段文本", "answer_candidate": "回答候选",
    "plan_candidate": "查询计划候选", "review_candidate": "计划复核意见",
    "text": "文本输出", "empty": "空输出", "error": "请求错误",
}


def model_messages(direction: str, messages: list, extras: dict | None = None) -> None:
    """OpenInference chat messages with one shared size/attribute budget per direction."""
    span = _scope.get()
    if span is None:
        return
    remaining, count, truncated = LIMIT, 0, False

    def put(key, value):
        nonlocal remaining, count, truncated
        if value is None:
            return
        safe = clean(value)
        text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False)
        if count >= MODEL_ATTRIBUTE_LIMIT or remaining <= 0:
            truncated = True
            return
        if len(text) > remaining:
            text = text[:remaining] + "\n[已截断；资料不完整，无附件]"
            truncated = True
        remaining -= len(text)
        span.set_attribute(key, text)
        count += 1

    try:
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                truncated = True
                continue
            prefix = f"llm.{direction}_messages.{i}.message"
            put(f"{prefix}.role", message.get("role"))
            content = message.get("content")
            if isinstance(content, list):
                for j, part in enumerate(content):
                    base = f"{prefix}.contents.{j}.message_content"
                    if isinstance(part, dict) and part.get("type") == "text":
                        put(f"{base}.type", "text")
                        put(f"{base}.text", part.get("text"))
                    else:
                        # Current loop is text-only; retain an explicit gap for new media.
                        put(f"{base}.type", "text")
                        put(f"{base}.text", "[未记录：不支持的消息内容类型]")
                        truncated = True
            else:
                put(f"{prefix}.content", content)
            put(f"{prefix}.name", message.get("name"))
            put(f"{prefix}.tool_call_id", message.get("tool_call_id"))
            for j, call in enumerate(message.get("tool_calls") or []):
                function = call.get("function") or {}
                base = f"{prefix}.tool_calls.{j}.tool_call"
                put(f"{base}.id", call.get("id"))
                put(f"{base}.function.name", function.get("name"))
                put(f"{base}.function.arguments", function.get("arguments"))
            # Preserve provider-returned fields without claiming hidden reasoning.
            other = {k: v for k, v in message.items() if k not in {
                "role", "content", "name", "tool_call_id", "tool_calls",
            } and v is not None}
            if other:
                put(f"trace.{direction}.message.{i}.extra", other)
        for key, value in (extras or {}).items():
            put(key, value)
        fields(**{f"trace.{direction}.message_count": len(messages),
                  f"trace.{direction}.truncated": truncated})
        if truncated:
            fields(**{f"trace.{direction}.notice": "已截断；消息或参数不完整，无附件"})
    except Exception:
        fields(**{f"trace.{direction}.incomplete": True})
        log.warning("Trace message mapping incomplete; business continues", exc_info=False)


def model_output(body: object, request: httpx.Request, failed: bool) -> None:
    """Classify submitted actions, never infer acceptance or semantic correctness."""
    span = _scope.get()
    if span is None:
        return
    try:
        stage = current_model_stage()
        choices = body.get("choices", []) if isinstance(body, dict) else []
        messages = [choice.get("message") or {} for choice in choices]
        extras = {"trace.response.metadata": {
            k: v for k, v in body.items() if k != "choices"
        }} if isinstance(body, dict) else {}
        extras["trace.response.finish_reasons"] = [c.get("finish_reason") for c in choices]
        model_messages("output", messages, extras)
        categories = []
        known_tools = request.extensions.get("local_trace_tools", ())
        for message in messages:
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                categories.append({"register_goals": "goal_registration",
                                   "query_complete": "query_completion"}.get(
                                       name, "business_tool_call" if name in known_tools
                                       else "other_tool_call"))
            if message.get("content"):
                categories.append({"query": "query_text", "answer": "answer_candidate",
                                   "tool_planning": "plan_candidate",
                                   "tool_review": "review_candidate"}.get(stage, "text"))
        categories = list(dict.fromkeys(categories)) or ["error" if failed else "empty"]
        if failed and "error" not in categories:
            categories.append("error")
        summary = "、".join(OUTPUT_LABELS[c] for c in categories)
        fields(**{"trace.output.categories": categories})
        model_output_summary(messages, categories, stage)
        # Short summaries make the trace list readable; full content lives only in messages.
        span.set_attribute("output.value", summary)
        span.set_attribute("output.mime_type", "text/plain")
        span.update_name(f"{request.extensions.get('local_trace_title', '模型调用')} → {summary}")
    except Exception:
        fields(**{"trace.output.incomplete": True})
        log.warning("Trace output classification incomplete; business continues", exc_info=False)


class ModelHTTPClient(httpx.AsyncClient):
    """Only the owned loop model client; SDK/transport retry attempts stay separate."""

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        if _scope.get() is None:
            return await super().send(request, **kwargs)
        label = "模型 · " + PURPOSES.get(current_model_stage(), "模型调用")
        with step(label) as span:
            request.extensions["local_trace_span"] = span
            try:
                response = await super().send(request, **kwargs)
                # Existing usage hook already reads the non-streaming body.
                try:
                    body = response.json()
                except ValueError:
                    body = {"unavailable": "non-JSON response"}
                model_output(body, request, not response.is_success)
                fields(http_status=response.status_code)
                usage = body.get("usage") if isinstance(body, dict) else None
                known = isinstance(usage, dict) and all(
                    type(usage.get(k)) is int for k in ("prompt_tokens", "completion_tokens")
                )
                fields(usage_status="known" if known else "unknown")
                if known:
                    fields(**{"llm.token_count.prompt": usage["prompt_tokens"],
                              "llm.token_count.completion": usage["completion_tokens"]})
                if not response.is_success and span is not None:
                    span.set_status(trace.StatusCode.ERROR, f"HTTP {response.status_code}")
                return response
            except BaseException as exc:
                fields(**{"trace.output.categories": [
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                ]})
                raise
            finally:
                if span is not None:
                    latest = _last_model.get()
                    if latest is not None:
                        latest["id"] = format(span.context.span_id, "016x")


def needs_answer_materials() -> bool:
    return _scope.get() is not None and not (_last_model.get() or {}).get("answer_recorded")


def model_request(request: httpx.Request, attempt: int) -> None:
    span = request.extensions.get("local_trace_span")
    if span is not None:
        try:
            body = json.loads(request.content)
            span.set_attribute("openinference.span.kind", "LLM")
            # Custom model attribute avoids Phoenix's built-in price estimates.
            fields(model=body.get("model"), stage=current_model_stage(), attempt=attempt,
                   usage_status="unknown")
            stage = current_model_stage()
            title = f"模型 · {PURPOSES.get(stage, '模型调用')} #{attempt}"
            request.extensions["local_trace_title"] = title
            request.extensions["local_trace_tools"] = {
                tool.get("function", {}).get("name") for tool in body.get("tools", [])
            } - {"register_goals", "query_complete"}
            span.update_name(title)
            fields(**{"trace.model.purpose": PURPOSES.get(stage, "模型调用"),
                      "trace.model.request_index": attempt})
            extras = {"llm.invocation_parameters": {
                k: v for k, v in body.items() if k not in {"messages", "tools"}
            }}
            for i, tool in enumerate(body.get("tools", [])):
                extras[f"llm.tools.{i}.tool.json_schema"] = tool
            model_messages("input", body.get("messages", []), extras)
            model_input_summary(body, stage, attempt)
            span.set_attribute("input.value", f"{title}；{len(body.get('messages', []))} 条消息")
            span.set_attribute("input.mime_type", "text/plain")
            if current_model_stage() == "answer" and _last_model.get() is not None:
                _last_model.get()["answer_recorded"] = True
        except Exception:
            log.warning("Trace model input lost; business continues", exc_info=False)
