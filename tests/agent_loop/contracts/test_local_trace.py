"""P4 correlation, rejected inputs, attempts and fail-open behavior, entirely offline."""

import asyncio
import json

import pytest
from agent_loop.support.loop_runtime import (
    final_answer,
    goal,
    register,
    runtime_fixture,
)
from agent_loop.support.trace_scenario import ANSWER, MESSAGE, scenario
from app.core import loop_tracing as tracing
from app.schemas.chat import ChatRequest
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytestmark = [pytest.mark.asyncio]


@pytest.fixture
def memory_traces(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        shutdown_on_exit=False, span_limits=SpanLimits(max_attributes=512),
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    yield exporter
    provider.shutdown()


async def test_attempts_inputs_handoff_and_model_parentage(monkeypatch, memory_traces):
    runtime, provider, repo, attempts = scenario(monkeypatch)
    result = await runtime.run(ChatRequest(message=MESSAGE), 41)
    spans = memory_traces.get_finished_spans()
    assert len({s.context.trace_id for s in spans}) == 1
    root = next(s for s in spans if s.name == "用户请求")
    models = [s for s in spans if s.attributes['openinference.span.kind'] == 'LLM']
    tools = [s for s in spans if s.attributes['openinference.span.kind'] == 'TOOL']
    assert len(models) == len(provider.requests) == 9
    assert len(tools) == len(attempts) == 3
    assert result.answer == ANSWER
    assert [json.loads(s.attributes['output.value'])['ok'] for s in tools] == [False, False, True]
    assert all(s.parent.span_id in {t.context.span_id for t in tools}
               for s in models if s.attributes['stage'] == 'tool_planning')
    assert sum(s.attributes['llm.token_count.prompt'] for s in models) == 99
    for span, request in zip(models, provider.requests, strict=True):
        for i, message in enumerate(request['messages']):
            prefix = f'llm.input_messages.{i}.message'
            assert span.attributes[f'{prefix}.role'] == message['role']
            if isinstance(message.get('content'), list):
                for j, part in enumerate(message['content']):
                    assert span.attributes[
                        f'{prefix}.contents.{j}.message_content.text'
                    ] == part['text']
            elif message.get('content') is not None:
                assert span.attributes[f'{prefix}.content'] == message['content']
            for j, call in enumerate(message.get('tool_calls') or []):
                base = f'{prefix}.tool_calls.{j}.tool_call'
                assert span.attributes[f'{base}.id'] == call['id']
                assert (
                    span.attributes[f'{base}.function.arguments'] == call['function']['arguments']
                )
        assert span.attributes['trace.input.truncated'] is False
        assert span.attributes['trace.output.truncated'] is False
        assert span.dropped_attributes == 0
        assert span.attributes['input.mime_type'] == 'text/plain'
        assert span.attributes['llm.output_messages.0.message.role'] == 'assistant'
    assert json.loads(models[0].attributes['trace.output.categories']) == [
        'goal_registration', 'business_tool_call',
    ]
    assert '目标登记提案、业务工具请求' in models[0].name
    assert json.loads(models[-2].attributes['trace.output.categories']) == ['query_completion']
    assert json.loads(models[-1].attributes['trace.output.categories']) == ['answer_candidate']
    assert models[-1].attributes['trace.model.request_index'] == 9
    overview = json.loads(models[0].attributes['metadata'])
    assert overview['purpose'] == '任务规划与调度'
    assert [a['action'] for a in overview['submitted_actions']] == [
        'register_goals', 'catalog_search',
    ]
    assert overview['submitted_actions'][1]['arguments']['limit'] == 'bad'
    tool_summary = json.loads(tools[-1].attributes['metadata'])
    assert tool_summary['tool_result']['ok'] is True
    assert tool_summary['result_window']['returned_count'] == 1
    assert json.loads(models[-1].attributes['metadata'])['input_overview']['goal_count'] == 1
    event = next(e for e in root.events if e.name == 'validation.rejected')
    assert 'bad' in event.attributes['submission'] and 'limit' in event.attributes['error']
    assert event.attributes['model_span_id'] in {format(s.context.span_id, '016x') for s in models}
    facts = json.loads(models[-1].attributes['llm.input_messages.1.message.content'])
    assert facts['goals'][0]['execution']['attempt_count'] == 3
    assert facts['evidence'][-1]['result']['output']['products'][0]['price'] == '299'
    assert 'model_stop_reason' not in facts
    assert json.loads(root.attributes['output.value']) == ANSWER
    assert repo.data['audit']['agent_loop']['stop_reason'] == 'completed'
    assert tracing._scope.get() is None


async def test_concurrent_and_disabled_results_match(monkeypatch, memory_traces):
    fixtures = [scenario(monkeypatch, enabled=enabled) for enabled in (False, True, True)]
    results = await asyncio.gather(*[
        runtime.run(ChatRequest(message=MESSAGE), 41) for runtime, *_ in fixtures
    ])
    assert [r.answer for r in results] == [ANSWER] * 3
    assert [len(p.requests) for _, p, _, _ in fixtures] == [9] * 3
    assert [len(attempts) for *_, attempts in fixtures] == [3] * 3
    spans = memory_traces.get_finished_spans()
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 2 and len({s.context.trace_id for s in spans}) == 2
    for root in roots:
        children = [s for s in spans if s.context.trace_id == root.context.trace_id]
        assert len(children) == 14
        ids = {s.context.span_id for s in children}
        assert all(s.parent is None or s.parent.span_id in ids for s in children)


async def test_redaction_snapshot_and_explicit_truncation(memory_traces):
    secret = tracing._secrets.set(('actual-secret-value',))
    try:
        raw = {'api_key': 'hidden', 'messages': [{'content':
            '{"password":"hidden2","email":"x@example.com"}'}],
            'malformed': '```json {"password": "private words",',
            'text': 'actual-secret-value Bearer abc postgres://user:pass@host/db 13800138000'}
        with tracing.step('safety', root=True):
            tracing.payload('input', raw)
            tracing.program_event('安全快照', outcome='recorded', data=raw)
            tracing.metadata(overview=raw)
            raw['api_key'] = 'mutated'
            tracing.payload('output', 'x' * 40000)
        span = memory_traces.get_finished_spans()[0]
        serialized = str(span.attributes) + str([e.attributes for e in span.events])
        assert all(v not in serialized for v in (
            'actual-secret-value', 'hidden', 'hidden2', 'x@example.com', 'user:pass', '13800138000',
            'private words',
        ))
        assert 'mutated' not in serialized
        assert json.loads(span.attributes['output.value'])['truncated'] is True
    finally:
        tracing._secrets.reset(secret)


async def test_export_failure_and_cancel_do_not_change_business(monkeypatch, caplog):
    class Broken(SpanExporter):
        def export(self, spans):
            raise OSError('synthetic offline collector')

    provider = TracerProvider(
        shutdown_on_exit=False, span_limits=SpanLimits(max_attributes=512),
    )
    provider.add_span_processor(SimpleSpanProcessor(tracing.SafeExporter(Broken())))
    monkeypatch.setattr(tracing, '_provider', provider)
    runtime, model, _repo, attempts = scenario(monkeypatch)
    result = await runtime.run(ChatRequest(message=MESSAGE), 41)
    assert result.answer == ANSWER and len(model.requests) == 9 and len(attempts) == 3
    assert 'spans lost' in caplog.text
    for enabled in (False, True):
        runtime.settings = runtime.settings.model_copy(update={'loop_trace_enabled': enabled})
        async def cancel(*args):
            raise asyncio.CancelledError
        monkeypatch.setattr(runtime.context, 'prepare_turn', cancel)
        with pytest.raises(asyncio.CancelledError):
            await runtime.run(ChatRequest(message=MESSAGE), 41)
    provider.shutdown()


async def test_answer_repair_and_unknown_usage(monkeypatch, memory_traces):
    message = '讲讲选择键盘的思路'
    runtime, provider, _ = runtime_fixture(monkeypatch, [
        register(goal(message, disposition='direct_response')),
        {'content': 'bad json'}, final_answer(),
    ])
    # Non-query completion in the first batch, then malformed answer and repair.
    from agent_loop.support.loop_runtime import calls
    provider.steps[0] = calls(
        *provider.steps[0]['calls'],
        ('query_complete', {'completion': {'reason': 'direct'}}),
    )
    runtime.settings = runtime.settings.model_copy(update={'loop_trace_enabled': True})
    original = provider.__class__.__call__
    def missing_usage(self, request):
        response = original(self, request)
        body = response.json()
        body.pop('usage')
        import httpx
        return httpx.Response(200, json=body)
    monkeypatch.setattr(provider.__class__, '__call__', missing_usage)
    await runtime.run(ChatRequest(message=message), 41)
    spans = memory_traces.get_finished_spans()
    models = [s for s in spans if s.attributes['openinference.span.kind'] == 'LLM']
    assert len(models) == 3
    assert all('llm.token_count.prompt' not in s.attributes for s in models)
    assert all(s.attributes['usage_status'] == 'unknown' for s in models)
    root = next(s for s in spans if s.parent is None)
    assert any('bad json' in e.attributes['submission'] for e in root.events)


async def test_cancellation_inside_tool_preserves_attempt(monkeypatch, memory_traces):
    from agent_loop.support.domain_tools import make_catalog
    from agent_loop.support.loop_runtime import calls
    from app.tools.contracts import BoundTool, ToolCatalog

    catalog = make_catalog(monkeypatch)

    async def cancelled(request):
        raise asyncio.CancelledError

    runtime, model, _repo = runtime_fixture(monkeypatch, [
        calls(*register(goal('查 SKU 9101'))['calls'],
              ('catalog_search', {'query': 'SKU 9101'})),
    ], catalog_factory=lambda _: ToolCatalog([
        BoundTool(b.contract, cancelled if b.contract.llm_name == 'catalog_search' else b.handler)
        for b in catalog.list_bound_tools()
    ]))
    runtime.settings = runtime.settings.model_copy(update={'loop_trace_enabled': True})
    with pytest.raises(asyncio.CancelledError):
        await runtime.run(ChatRequest(message='查 SKU 9101'), 41)
    spans = memory_traces.get_finished_spans()
    tool = next(s for s in spans if s.attributes.get('tool.name') == 'catalog_search')
    assert json.loads(tool.attributes['output.value'])['error']['code'] == 'cancelled'
    assert tool.attributes['outcome'] == 'cancelled'
    root = next(s for s in spans if s.parent is None)
    assert root.attributes['outcome'] == 'cancelled'
    assert len(model.requests) == 1
    assert tracing._scope.get() is None


async def test_local_exporter_ignores_ambient_credentials_and_cloud(monkeypatch, memory_traces):
    import requests

    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'https://cloud.invalid')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'Authorization=secret-value')
    sent = []

    def send(session, request, **kwargs):
        sent.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    with tracing.step('synthetic', root=True):
        tracing.payload('input', {'message': 'synthetic'})
    exporter = tracing.local_exporter()
    try:
        assert exporter.export(memory_traces.get_finished_spans()) == SpanExportResult.SUCCESS
    finally:
        exporter.shutdown()
    request, kwargs = sent[0]
    assert request.url == 'http://127.0.0.1:6006/v1/traces'
    assert set(request.headers) == {'Content-Type'}
    assert kwargs['allow_redirects'] is False and kwargs['proxies'] == {}


async def test_message_display_limits_redaction_and_classification(memory_traces):
    import httpx
    from app.core.loop_budget import model_stage

    with tracing.step('display', root=True):
        tracing.model_messages('input', [
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"password":"secret-123"}'},
            {'role': 'user', 'content': 'x' * 40000},
        ])
        with model_stage('query'):
            tracing.model_output({'choices': [{'message': {
                'role': 'assistant', 'content': 'untrusted text', 'tool_calls': [{
                    'id': 'call-2', 'function': {'name': 'unknown', 'arguments': '{}'},
                }],
            }, 'finish_reason': 'tool_calls'}]}, httpx.Request('POST', 'https://invalid'), False)
    span = memory_traces.get_finished_spans()[0]
    assert 'secret-123' not in str(span.attributes)
    assert span.attributes['llm.input_messages.0.message.tool_call_id'] == 'call-1'
    assert span.attributes['trace.input.truncated'] is True
    assert '已截断' in span.attributes['llm.input_messages.1.message.content']
    assert json.loads(span.attributes['trace.output.categories']) == [
        'other_tool_call', 'query_text',
    ]
    with tracing.step('many-messages', root=True):
        tracing.model_messages('input', [{'role': 'user', 'content': 'short'}] * 150)
    many = memory_traces.get_finished_spans()[-1]
    assert many.attributes['trace.input.truncated'] is True
    assert many.dropped_attributes == 0
    assert sum(k.startswith('llm.input_messages.') for k in many.attributes) == 180


async def test_catalog_plan_review_and_program_decisions_match_execution(
    monkeypatch, memory_traces,
):
    from agent_loop.support.domain_tools import make_catalog
    from agent_loop.support.loop_runtime import calls, finish
    from app.tools.catalog_search.planner import CatalogSearchPlanner
    from app.tools.catalog_search.service import CatalogSearchService
    from app.tools.contracts import BoundTool, ToolCatalog

    from tests.support.tool_plan_fixtures import search_plan
    from tests.support.tool_planning import Repository, product

    query = '查200元以内的键盘'
    plan = search_plan({'category': '键盘', 'conditions': [
        {'key': 'price', 'operator': 'lte', 'values': ['200'], 'evidence': '200元以内'},
    ]})
    def review(path):
        return {'verdict': 'approve', 'checks': [{
            'evidence': '200元以内', 'plan_paths': [path],
            'status': 'matched', 'explanation': '合成复核',
        }]}
    runs = []
    for enabled in (False, True):
        repo = Repository([product(1, price='100'), product(2, price='300')])
        catalog = make_catalog(monkeypatch)
        def factory(model, repo=repo, catalog=catalog):
            service = CatalogSearchService(repo, CatalogSearchPlanner(model))
            return ToolCatalog([
                BoundTool(b.contract, service.search if b.contract.llm_name == 'catalog_search'
                          else b.handler) for b in catalog.list_bound_tools()
            ])
        runtime, model, _ = runtime_fixture(monkeypatch, [
            calls(*register(goal(query))['calls'], ('catalog_search', {'query': query})),
            {'content': json.dumps(plan)},
            {'content': json.dumps(review('/nonexistent'))},
            {'content': json.dumps(review('/conditions'))},
            finish(), final_answer(),
        ], catalog_factory=factory)
        runtime.settings = runtime.settings.model_copy(update={'loop_trace_enabled': enabled})
        result = await runtime.run(ChatRequest(message=query), 41)
        runs.append((result.answer, len(model.requests), len(repo.calls)))
    assert runs[0] == runs[1]
    assert runs[1][1:] == (6, 2)
    spans = memory_traces.get_finished_spans()
    tool = next(s for s in spans if s.attributes.get('tool.name') == 'catalog_search')
    events = tool.events
    rejected = next(e for e in events if e.name == '程序 · 计划或复核输出校验')
    details = json.loads(rejected.attributes['details'])
    assert details['phase'] == 'review'
    assert rejected.attributes['outcome'] == 'rejected'
    repair = next(e for e in events if e.name == '程序 · 安排输出修正')
    assert json.loads(repair.attributes['details'])['next_action'] == 'repair_review'
    models = [s for s in spans if s.attributes.get('stage') == 'tool_review']
    assert len(models) == 2
    assert rejected.attributes['model_span_id'] == format(models[0].context.span_id, '016x')
    assert all('工具计划复核' in s.name for s in models)
    validations = [s for s in spans if s.attributes.get('record_type') == 'program_validation']
    assert [s.name for s in validations] == [
        '程序 · 计划校验【通过】', '程序 · 复核输出校验【拒绝】',
        '程序 · 复核输出校验【通过】', '程序 · 工具输出校验【通过】',
    ]
    assert all(s.parent.span_id == tool.context.span_id for s in validations)
    assert validations[1].status.status_code.name == 'ERROR'
    assert validations[1].attributes['model_span_id'] == format(models[0].context.span_id, '016x')
    assert 'Review path must reference' in (
        json.loads(validations[1].attributes['metadata'])['error']
    )
    assert validations[2].attributes['model_span_id'] == format(models[1].context.span_id, '016x')
    assert all(s.attributes['openinference.span.kind'] == 'CHAIN' for s in validations)

    checks = json.loads(
        next(e for e in events if e.name == '程序 · 条件筛选').attributes['details']
    )
    assert checks['identity_sku_count'] == 2
    assert checks['exact_sku_count'] == 1 and checks['mismatch_sku_count'] == 1
    ready = next(e for e in events if e.name == '程序 · 计划就绪')
    assert json.loads(ready.attributes['details'])['plan']['conditions'][0]['values'] == ['200']
    root = next(s for s in spans if s.parent is None)
    projection = next(e for e in root.events if e.name == '程序 · 材料投影')
    assert json.loads(projection.attributes['details'])['destination'] == 'answer'
    assert projection.attributes['outcome'] == 'kept'
    overview = json.loads(tool.attributes['metadata'])
    assert overview['last_validation_rejection']['phase'] == 'review'
    assert overview['review_result']['verdict'] == 'approve'
    assert overview['tool_result']['ok'] is True


async def test_projection_event_explains_omission_without_changing_evidence(memory_traces):
    from datetime import UTC, datetime

    from app.agent.loop.evidence import EvidenceStore, ToolInvocation
    from app.agent.loop.query_loop import project_record
    from app.tools.contracts import ToolExecutionResult

    now = datetime.now(UTC)
    record = EvidenceStore().record(
        ToolInvocation(invocation_id='a1', operation_id='op1', goal_id='g1',
                       tool_name='catalog_search', arguments={}, attempt=1, started_at=now,
                       elapsed_seconds=0.1, status='succeeded'),
        ToolExecutionResult(tool_name='catalog_search', ok=True, output={
            'status': 'success', 'products': [{'title': 'large' * 4000}],
            'returned_count': 1, 'total_match_count': 1, 'is_exhaustive': True,
        }), now,
    )
    with tracing.step('projection', root=True):
        result = project_record(record, destination='answer')
    event = memory_traces.get_finished_spans()[0].events[0]
    details = json.loads(event.attributes['details'])
    assert event.attributes['outcome'] == 'truncated'
    assert details['source_product_count'] == 1 and details['retained_product_count'] == 0
    assert details['original_chars'] > details['limit_chars'] == 16000
    assert details['projected_chars'] < 16000 and 'products' in details['omitted_fields']
    assert details['invocation_id'] == 'a1'
    assert 'products' not in result['result']['output']
    assert record.result.output['products'][0]['title'] == 'large' * 4000


async def test_metadata_limit_does_not_replace_full_messages(memory_traces):
    with tracing.step('overview-limit', root=True):
        tracing.model_messages('input', [{'role': 'user', 'content': 'full evidence'}])
        tracing.metadata(overview='x' * 5000)
    span = memory_traces.get_finished_spans()[0]
    overview = json.loads(span.attributes['metadata'])
    assert overview['summary_truncated'] is True
    assert len(overview['preview']) <= 3800
    assert span.attributes['llm.input_messages.0.message.content'] == 'full evidence'


async def test_tool_output_validation_span_preserves_rejection(monkeypatch, memory_traces):
    from agent_loop.support.domain_tools import make_catalog
    from app.tools.contracts import BoundTool, execute_bound_tool

    contract = make_catalog(monkeypatch).get_by_llm_name('catalog_search').contract
    async def malformed(request):
        return {'not': 'a schema model'}

    bound = BoundTool(contract, malformed)
    disabled = await execute_bound_tool(bound, {'query': 'SKU 9101'}, {'user_id': 41})
    with tracing.step('工具 · catalog_search', 'TOOL', root=True):
        enabled = await execute_bound_tool(bound, {'query': 'SKU 9101'}, {'user_id': 41})
    assert enabled == disabled and not enabled.ok
    validation = memory_traces.get_finished_spans()[0]
    assert validation.name == '程序 · 工具输出校验【拒绝】'
    assert validation.attributes['validation_outcome'] == 'rejected'
    assert validation.status.status_code.name == 'ERROR'
    assert 'structured output model' in json.loads(validation.attributes['metadata'])['error']
