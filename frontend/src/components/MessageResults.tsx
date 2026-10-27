import { useState } from "react";
import { BookOpenText, ExternalLink, Package, Truck } from "lucide-react";
import type { ChatMessage } from "../types";
import { OrderCardView, OrderDetailContent, OrderSummaryCard } from "./OrderCards";
import { DetailDrawer } from "./DetailDrawer";
import { formatDateTime } from "./common";

export function MessageResults({ message, onAsk, disabled }: {
  message: ChatMessage; onAsk?: (text: string) => void; disabled?: boolean;
}) {
  const [orderPanel, setOrderPanel] = useState<"all" | "detail" | null>(null);
  const orders = (message.orders ?? []).filter(item => item.id !== message.order?.id);
  const count = orders.length + (message.order ? 1 : 0);
  const previewOrders = orders.slice(0, message.order ? 1 : 2);
  const askOrder = (id: number) => {
    setOrderPanel(null);
    onAsk?.(`请查询订单 ${id} 的商品明细和最新物流。`);
  };
  const summaryCards = (items: typeof orders, full = false) => items.map((order, index) => <div key={order.id}>
    <OrderSummaryCard order={order} index={index} full={full} />
    {onAsk && <button className="text-button order-followup" disabled={disabled} type="button"
      onClick={() => askOrder(order.id)}>询问明细与最新物流<Truck size={14} /></button>}
  </div>);
  return <>
    {(message.order || orders.length > 0 || message.orderQuery) && <section className="message-orders">
      <div className="result-heading"><strong><Package size={16} />订单查询</strong>
        {count > 2 ? <button className="text-button" type="button" onClick={() => setOrderPanel("all")}>查看本次全部 {count} 笔</button>
          : <span>{formatDateTime(message.createdAt)}</span>}
      </div>
      {message.orderQuery && <p className="result-caption">匹配 {message.orderQuery.total_match_count} 笔，本次返回 {message.orderQuery.returned_count} 笔
        {!message.orderQuery.is_exhaustive && " · 当前为部分结果"}</p>}
      {message.order && <OrderCardView order={message.order} onOpen={() => setOrderPanel("detail")} />}
      {summaryCards(previewOrders)}
      {count <= 2 && orders.length > 0 && <button className="text-button order-followup" type="button" onClick={() => setOrderPanel("all")}>查看订单记录</button>}
      <small className="order-results-caption">以上为本次查询时的信息。</small>
    </section>}
    {orderPanel && <DetailDrawer title={orderPanel === "detail" ? "订单与物流" : `本次返回的 ${count} 笔订单`} onClose={() => setOrderPanel(null)}>
      {orderPanel === "detail" && message.order ? <>
        {orders.length > 0 && <button className="text-button" type="button" onClick={() => setOrderPanel("all")}>返回订单列表</button>}
        <OrderDetailContent order={message.order} />
        {onAsk && <button className="text-button order-followup" disabled={disabled} type="button" onClick={() => askOrder(message.order!.id)}>询问最新明细与物流<Truck size={14} /></button>}
      </> : <>
        {message.order && <OrderCardView order={message.order} onOpen={() => setOrderPanel("detail")} />}
        {summaryCards(orders, true)}
      </>}
      <p className="result-caption">以上为本次查询时的信息。</p>
    </DetailDrawer>}
    {!!message.evidence?.length && <details className="source-group">
      <summary><BookOpenText size={17} /><strong>相关资料</strong><span>{message.evidence.length} 条</span></summary>
      <div className="source-list">{message.evidence.map((item, index) => {
        const url = item.source_type === "web_page" && /^https?:\/\//i.test(item.url ?? "") ? item.url : null;
        return <article className="source-card" key={`${item.source_type}-${item.source_id}-${index}`}>
          <span className="eyebrow">{item.source_type === "web_page" ? "外部知识资料" : "本店政策资料"}</span>
          {url ? <a href={url} target="_blank" rel="noopener noreferrer">{item.title}<ExternalLink size={14} /></a> : <strong>{item.title}</strong>}
          <p>{item.snippet}</p>
        </article>;
      })}</div>
    </details>}
  </>;
}
