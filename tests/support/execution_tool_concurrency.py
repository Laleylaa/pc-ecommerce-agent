import asyncio


class SessionFactory:
    def __init__(self):
        self.opened = []
        self.closed = []

    def __call__(self):
        factory = self
        session = object()

        class Scope:
            async def __aenter__(self):
                factory.opened.append(session)
                return session

            async def __aexit__(self, *exc):
                factory.closed.append(session)

        return Scope()


def wave_fixture(execute, names=None, *, limit=3):
    from types import SimpleNamespace

    from app.agent.legacy.execution import execute_tool_wave
    from app.tools.contracts import DefaultToolContractProvider

    names = names or ["order_lookup"] * 4
    calls = [{"id": f"call_{i}", "subquery": f"task_{i}", "name": name,
              "arguments": {"query": str(i)}} for i, name in enumerate(names)]
    state = {"run_id": 1, "user_id": 7,
             "decision": {"type": "tool_calls", "tool_calls": calls}}
    runtime = SimpleNamespace(
        session=object(), tool_concurrency_limit=limit,
        contract_provider=DefaultToolContractProvider(),
        tool_executor=SimpleNamespace(execute=execute, supports_concurrent_calls=True),
        _prepare_tool_call=lambda state, call: (call, [int(call.arguments["query"])]),
    )
    persisted, events = [], []

    class Audit:
        active = False

        def __init__(self, session):
            assert session is runtime.session

        async def add_tool_call(self, run_id, name, arguments, output):
            assert not self.active, "audit session used concurrently"
            self.active = True
            await asyncio.sleep(0)
            persisted.append((name, arguments, output))
            self.active = False

    async def run():
        return await execute_tool_wave(
            runtime, state, repository_factory=Audit, stream_writer_factory=lambda: events.append,
        )

    return run, state, runtime, persisted, events


def success(name="order_lookup"):
    from app.tools.schemas import ToolExecutionResult

    return ToolExecutionResult(tool_name=name, ok=True, output={})
