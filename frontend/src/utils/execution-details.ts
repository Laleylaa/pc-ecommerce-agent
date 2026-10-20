import type { ExecutionDetails, ExecutionToolCall } from "../types";

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) => typeof value === "string" ? value : "";

// Persisted pre-refactor turns can lack fields added to the current response contract.
export function executionDetailsFromMetadata(value: unknown): ExecutionDetails | undefined {
  if (!record(value) || !Array.isArray(value.goals) || !Array.isArray(value.tool_calls)) return;
  return {
    goals: value.goals.filter(record).map((goal, index) => ({
      id: text(goal.id) || `history-goal-${index}`,
      query: text(goal.query), disposition: text(goal.disposition),
      task_ids: Array.isArray(goal.task_ids)
        ? goal.task_ids.filter((id): id is string => typeof id === "string") : []
    })),
    tool_calls: value.tool_calls.filter(record).map((call, index): ExecutionToolCall => ({
      id: text(call.id) || `history-call-${index}`, task_id: text(call.task_id),
      tool_name: text(call.tool_name), query: text(call.query),
      outcome: text(call.outcome), reason: text(call.reason),
      used_in_answer: call.used_in_answer === true, superseded: call.superseded === true,
      reused: call.reused === true, arguments: record(call.arguments) ? call.arguments : {},
      result_summary: record(call.result_summary) ? call.result_summary : {}
    })),
    model_calls: record(value.model_calls) ? Object.fromEntries(
      Object.entries(value.model_calls).filter((entry): entry is [string, number] =>
        typeof entry[1] === "number" && Number.isInteger(entry[1]) && entry[1] >= 0)
    ) : null
  };
}
