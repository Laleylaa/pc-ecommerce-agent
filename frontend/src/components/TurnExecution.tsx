import type { EvidenceItem, ExecutionDetails, ExecutionToolCall } from "../types";

const dispositions: Record<string, string> = {
  tool_planning: "工具查询", direct_response: "直接回答",
  session_grounded_response: "依据对话回答", clarification: "需要澄清",
  human_handoff: "人工诉求", out_of_scope: "超出范围", security_refusal: "安全拒绝"
};
const outcomes: Record<string, string> = {
  usable: "取得可用资料", empty: "无匹配结果", not_found: "未找到对象",
  unsupported: "能力不支持", insufficient: "资料不足 / 需澄清", error: "调用失败"
};
const stages: Record<string, string> = {
  route_request: "目标理解", submit_execution_plan: "任务规划",
  tool_review: "工具语义检查", tool_planning: "工具内部规划", summarize_goals: "回答生成", answer: "回答生成",
  other: "其他"
};

export function TurnExecution({ details, evidence = [] }: {
  details?: ExecutionDetails | null;
  evidence?: EvidenceItem[];
}) {
  if (!details) return (
    <details className="turn-execution">
      <summary><span>执行详情</span><small>执行统计未记录 · 参考资料 {evidence.length} 条</small></summary>
      <div className="execution-body"><ReferenceMaterials evidence={evidence} /></div>
    </details>
  );
  const modelCount = details.model_calls == null ? null
    : Object.values(details.model_calls).reduce((sum, count) => sum + count, 0);
  const reusedCount = details.tool_calls.filter((call) => call.reused).length;
  const callCount = details.tool_calls.length - reusedCount;
  const taskIds = new Set(details.goals.flatMap((goal) => goal.task_ids));
  const earlierCalls = details.tool_calls.filter((call) => !taskIds.has(call.task_id));
  return (
    <details className="turn-execution">
      <summary>
        <span>执行详情</span>
        <small>{details.goals.length} 个子目标 · {callCount} 次工具调用{reusedCount > 0 ? ` · 复用 ${reusedCount} 次` : ""} · {modelCount === null ? "模型次数未记录" : `模型 ${modelCount} 次`} · 参考资料 {evidence.length} 条</small>
      </summary>
      <div className="execution-body">
        {details.goals.length === 0 && <p>本轮未生成子目标计划。</p>}
        {details.goals.map((goal, index) => {
          const calls = details.tool_calls.filter((call) => goal.task_ids.includes(call.task_id));
          return (
            <section className="execution-goal" key={goal.id}>
              <header><strong>子目标 {index + 1}</strong><span>{dispositions[goal.disposition] ?? goal.disposition}</span></header>
              <p>{goal.query}</p>
              {calls.length ? calls.map((call) => <ToolCall key={call.id} call={call} />)
                : <small>{goal.disposition === "tool_planning" ? "未记录到工具调用结果" : "此目标没有工具调用"}</small>}
            </section>
          );
        })}
        {earlierCalls.length > 0 && <section className="execution-goal">
          <strong>其他调用 / 恢复前记录</strong>
          {earlierCalls.map((call) => <ToolCall key={call.id} call={call} />)}
        </section>}
        <div className="execution-models">
          <strong>模型调用{modelCount !== null ? ` · ${modelCount} 次` : " · 未记录"}</strong>
          {Object.entries(details.model_calls ?? {}).map(([stage, count]) => (
            <span key={stage}>{stages[stage] ?? stage}：{count} 次</span>
          ))}
          {modelCount === 0 && <span>本轮没有发起模型调用。</span>}
          <small>按实际发起次数统计，包含失败、重试和恢复；不代表模型内部思考步数。</small>
        </div>
        <ReferenceMaterials evidence={evidence} />
      </div>
    </details>
  );
}

function ReferenceMaterials({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <section className="execution-references">
      <strong>参考资料 · {evidence.length} 条</strong>
      <small>本次返回的资料，不代表回答逐条引用。</small>
      {evidence.length === 0 ? <small>本次没有返回参考资料。</small> : evidence.map((item, index) => {
        const web = item.source_type === "web_page";
        const url = web && /^https?:\/\//i.test(item.url ?? "") ? item.url : null;
        return (
          <article key={`${item.source_type}-${item.source_id}-${index}`}>
            <small>{web ? "网页搜索" : "本店知识库"}</small>
            {url ? <a href={url} target="_blank" rel="noopener noreferrer">{item.title}</a>
              : <span>{item.title}</span>}
            {item.snippet && <p>{item.snippet}</p>}
          </article>
        );
      })}
    </section>
  );
}

function ToolCall({ call }: { call: ExecutionToolCall }) {
  return (
    <details className={`execution-call${call.outcome === "error" ? " execution-call-error" : ""}`}>
      <summary>
        <code>{call.tool_name}</code>
        <span>{outcomes[call.outcome] ?? call.outcome}{call.reused ? " · 复用结果" : ""}{call.superseded ? " · 已被替代" : ""}{call.used_in_answer ? " · 回答已引用" : ""}</span>
      </summary>
      {call.query && <p>{call.query}</p>}
      <small>结果原因：{call.reason || "未记录"}</small>
      <h4>调用参数</h4>
      <pre>{JSON.stringify(call.arguments, null, 2)}</pre>
      <h4>结果摘要</h4>
      <pre>{Object.keys(call.result_summary).length ? JSON.stringify(call.result_summary, null, 2) : "未取得结构化结果"}</pre>
    </details>
  );
}
