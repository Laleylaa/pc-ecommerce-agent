from typing import Any

from langgraph.graph import END, StateGraph

from app.agent.state import AgentState


def build_agent_graph(runtime: Any):
    """Compile the production graph from Runtime node implementations.

    Keeping topology here separates graph wiring from node behavior without introducing a
    service boundary or any additional runtime I/O.
    """
    workflow = StateGraph(AgentState)
    workflow.add_node("load_context", runtime._load_context)
    workflow.add_node("request_router", runtime._request_route)
    workflow.add_node("orchestrate", runtime._orchestrate)
    workflow.add_node("execute_tool_wave", runtime._execute_tool_wave)
    workflow.add_node("normalize_tool_results", runtime._normalize_tool_results)
    workflow.add_node("extract_task_artifacts", runtime._extract_task_artifacts)
    workflow.add_node("update_subquery_ledger", runtime._update_subquery_ledger)
    workflow.add_node("terminal_guard", runtime._terminal_guard)
    workflow.add_node("finalize_response", runtime._finalize_response)
    workflow.add_node("render_handoff_template", runtime._render_handoff_template)
    workflow.add_node("render_out_of_scope_template", runtime._render_out_of_scope_template)
    workflow.add_node("render_security_template", runtime._render_security_template)
    workflow.add_node("render_clarification_template", runtime._render_clarification_template)
    workflow.add_node("render_direct_template", runtime._render_direct_template)
    workflow.add_node(
        "render_session_grounded_response",
        runtime._render_session_grounded_response,
    )
    workflow.add_node("persist_turn", runtime._persist_turn)

    workflow.set_entry_point("load_context")
    workflow.add_edge("load_context", "request_router")
    workflow.add_conditional_edges(
        "request_router",
        runtime._dispatch_route,
        {
            "plan": "orchestrate",
            "human_handoff": "render_handoff_template",
            "out_of_scope": "render_out_of_scope_template",
            "security_refusal": "render_security_template",
            "clarification": "render_clarification_template",
            "direct_response": "render_direct_template",
            "session_grounded_response": "render_session_grounded_response",
        },
    )
    workflow.add_conditional_edges(
        "orchestrate",
        runtime._dispatch_decision,
        {"execute": "execute_tool_wave", "guard": "terminal_guard"},
    )
    workflow.add_edge("execute_tool_wave", "normalize_tool_results")
    workflow.add_edge("normalize_tool_results", "extract_task_artifacts")
    workflow.add_edge("extract_task_artifacts", "update_subquery_ledger")
    workflow.add_edge("update_subquery_ledger", "orchestrate")
    workflow.add_conditional_edges(
        "terminal_guard",
        runtime._dispatch_terminal_guard,
        {"replan": "orchestrate", "respond": "finalize_response"},
    )
    for terminal_node in (
        "finalize_response",
        "render_handoff_template",
        "render_out_of_scope_template",
        "render_security_template",
        "render_clarification_template",
        "render_direct_template",
        "render_session_grounded_response",
    ):
        workflow.add_edge(terminal_node, "persist_turn")
    workflow.add_edge("persist_turn", END)
    return workflow.compile()


__all__ = ["build_agent_graph"]
