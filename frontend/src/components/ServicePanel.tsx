import { useState } from "react";
import { Loader2 } from "lucide-react";
import type { HandoffNotice, HandoffRequest, HandoffRequestType, MemoryItem } from "../types";
import { formatDateTime } from "./common";

export function PreferencesPanel({ memories, loading, error, forgetting, onForget }: {
  memories: MemoryItem[]; loading: boolean; error: string | null; forgetting: ReadonlySet<number>; onForget: (id: number) => void;
}) {
  return <section><p className="muted">已记住的偏好用于后续咨询。你的当前要求始终优先，也可以随时让助手忘记。</p>
    {loading && <p>正在加载偏好…</p>}{error && <p role="alert">{error}</p>}
    {!loading && !error && !memories.length && <div className="empty-state">还没有已记住的偏好</div>}
    {memories.map(item => <article className="memory-item" key={item.id}><div><strong>{item.display_value}</strong><small>{formatDateTime(item.updated_at)}</small></div>
      <button className="text-button" type="button" disabled={forgetting.has(item.id)} onClick={() => onForget(item.id)}>{forgetting.has(item.id) ? "处理中" : "忘记"}</button></article>)}
  </section>;
}
const statusLabels = { pending: "待人工确认", acknowledged: "已确认", resolved: "已解决" };
export function HandoffPanel({ notice, type, reason, loading, queryId, queryLoading, queryError, queryResult,
  onType, onReason, onSubmit, onQueryId, onQuery }: {
  notice: HandoffNotice | null; type: HandoffRequestType; reason: string; loading: boolean;
  queryId: string; queryLoading: boolean; queryError: string | null; queryResult: HandoffRequest | null;
  onType: (value: HandoffRequestType) => void; onReason: (value: string) => void; onSubmit: () => void;
  onQueryId: (value: string) => void; onQuery: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  return <section className="service-form">
    <p className="service-notice">这里可以记录你的诉求，等待人工确认。提交不会自动办理退款、退货、维修或订单修改。</p>
    {notice?.requested ? <div className="request-receipt"><strong>诉求已记录 · #{notice.requestId}</strong>
      <p>{notice.status ? statusLabels[notice.status] : "待人工确认"}</p><p>{notice.message}</p></div> : <>
      <p>{notice?.orderId ? `关联订单：#${notice.orderId}` : "本次未关联订单"}</p>
      <label>诉求类型<select value={type} disabled={loading} onChange={event => { onType(event.target.value as HandoffRequestType); setConfirmed(false); }}>
        <option value="return">退货</option><option value="refund">退款</option><option value="repair">维修</option><option value="order_change">订单修改</option><option value="other">其他</option>
      </select></label>
      <label>具体诉求<textarea rows={5} value={reason} disabled={loading} placeholder="请描述需要人工处理的问题"
        onChange={event => { onReason(event.target.value); setConfirmed(false); }} /></label>
      <label className="confirmation-check"><input type="checkbox" checked={confirmed} disabled={loading} onChange={event => setConfirmed(event.target.checked)} />
        <span>我已核对关联订单与诉求内容，确认提交。</span></label>
      <button className="primary-button" type="button" disabled={loading || !confirmed || !reason.trim()} onClick={onSubmit}>
        {loading && <Loader2 size={16} className="spin" />}确认提交诉求</button>
    </>}
    <div className="request-query"><h3>查询请求状态</h3><label>请求编号<input inputMode="numeric" value={queryId} onChange={event => onQueryId(event.target.value)} placeholder="输入请求编号" /></label>
      <button type="button" className="secondary-button" disabled={queryLoading || !queryId.trim()} onClick={onQuery}>{queryLoading ? "查询中…" : "查询状态"}</button>
      {queryError && <p role="alert">{queryError}</p>}{queryResult && <p>#{queryResult.id} · {statusLabels[queryResult.status]}<br />{formatDateTime(queryResult.updated_at)}</p>}
    </div>
  </section>;
}
