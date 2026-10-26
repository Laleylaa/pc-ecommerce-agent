"""Explicit synthetic runtime -> local Phoenix smoke; never collected by pytest.

All normal offline guards remain installed. Only the exact numeric loopback OTLP
port is admitted in this standalone process; there is no arbitrary URL option.
"""

# Offline guards must be installed before importing application modules.
# ruff: noqa: E402

import argparse
import asyncio
import json
import socket
from pathlib import Path
from time import perf_counter

# Save originals before installing offline guards; never change shared test protection.
_connect = socket.socket.connect
_getaddrinfo = socket.getaddrinfo

# isort: off
from tests.support import offline

from agent_loop.support.trace_scenario import ANSWER, MESSAGE, scenario
from app.core import loop_tracing as tracing
from app.schemas.chat import ChatRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pytest import MonkeyPatch
# isort: on



def allow_local_receiver():
    def connect(sock, address):
        if address != ('127.0.0.1', 6006):
            return offline.denied()
        return _connect(sock, address)

    def resolve(host, port, *args, **kwargs):
        if host != '127.0.0.1' or port != 6006:
            return offline.denied()
        return _getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.getaddrinfo = resolve


async def run(export):
    results = []
    exporter = InMemorySpanExporter()
    if export:
        allow_local_receiver()
    provider = tracing.provider() if export else TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with MonkeyPatch.context() as patch:
        patch.setattr(tracing, '_provider', provider)
        # Warm SDK imports without recording; one measured pair is not a benchmark.
        warm, *_ = scenario(patch, enabled=False)
        await warm.run(ChatRequest(message=MESSAGE), 41)
        for enabled in (False, True):
            runtime, model, _repo, attempts = scenario(patch, enabled=enabled)
            started = perf_counter()
            response = await runtime.run(ChatRequest(message=MESSAGE), 41)
            elapsed = perf_counter() - started
            assert response.answer == ANSWER
            assert len(model.requests) == 7 and len(attempts) == 2
            assert [c.outcome for c in response.execution_details.tool_calls] == [
                'error', 'error', 'usable',
            ]
            results.append({'tracing': enabled, 'seconds': round(elapsed, 6),
                            'model_requests': len(model.requests), 'tool_attempts': len(attempts),
                            'answer': response.answer})
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.parent is None)
        report = {'mode': 'synthetic-only', 'trace_id': format(root.context.trace_id, '032x'),
                  'spans': len(spans), 'comparison': results, 'exported': False,
                  'browser_accepted': False, 'real_llm': False, 'business_database': False,
                  'approx_span_json_bytes': sum(len(s.to_json().encode()) for s in spans)}
        if export:
            # Exercise the real bounded batch exporter, then verify the local receiver.
            provider.force_flush(timeout_millis=2000)
            import requests

            url = "http://127.0.0.1:6006/v1/projects/pc-ecommerce-agent-local/spans"
            with requests.Session() as client:
                client.trust_env = False
                for _ in range(10):
                    try:
                        result = client.get(url, params={"trace_id": report['trace_id']},
                                            timeout=2, allow_redirects=False)
                        result.raise_for_status()
                        received = result.json()['data']
                        if len(received) == len(spans):
                            models = [s for s in received if s['span_kind'] == 'LLM']
                            assert len(models) == 7
                            for model in models:
                                attrs = model['attributes']
                                assert 'llm.input_messages.0.message.role' in attrs
                                assert 'llm.output_messages.0.message.role' in attrs
                                assert attrs['trace.input.truncated'] is False
                                assert attrs['trace.output.truncated'] is False
                                assert ' → ' in model['name']
                                assert attrs['metadata.purpose']
                            report['openinference_messages_verified'] = True
                            tools = sorted((s for s in received if s['span_kind'] == 'TOOL'),
                                           key=lambda s: s['start_time'])
                            assert [json.loads(s['attributes']['output.value'])['ok']
                                    for s in tools] == [False, False, True]
                            event_names = [e['name'] for s in received
                                           for e in s.get('events', [])]
                            assert '程序 · 执行计划确认' in event_names
                            assert '程序 · 条件筛选' in event_names
                            assert '程序 · 材料投影' in event_names
                            report['program_events_verified'] = True
                            report['metadata_verified'] = True
                            report['exported'] = report['platform_api_verified'] = True
                            break
                    except (requests.RequestException, ValueError, KeyError):
                        pass
                    await asyncio.sleep(0.1)
        output = Path('artifacts/tests/phoenix-demo/report.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print('Phoenix project: pc-ecommerce-agent-local; http://127.0.0.1:6006')
        print(f'Report: {output}')
    provider.shutdown()
    if export and not report['exported']:
        raise SystemExit(
            'Platform verification failed; UI acceptance incomplete. '
            'Check trace-status and receiver data.'
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='memory collector only; no sockets')
    args = parser.parse_args()
    asyncio.run(run(not args.offline))
