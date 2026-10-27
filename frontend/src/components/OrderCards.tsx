import { ChevronDown, Package, Truck } from "lucide-react";
import { displayValue, formatDateTime } from "./common";
import { formatSpecs } from "../utils/spec-display";
import type { OrderCard, OrderSummary } from "../types";

// Trace timestamps are not guaranteed to arrive in chronological order.
// If any timestamp is missing/invalid, preserve source order without claiming a latest event.
export function orderLogisticsEvents(trace: Array<Record<string, string>>) {
  const canSort = trace.every(event => event.time && Number.isFinite(Date.parse(event.time)));
  return {
    events: canSort ? [...trace].sort((a, b) => Date.parse(b.time) - Date.parse(a.time)) : [...trace],
    latestKnown: canSort && trace.length > 0,
  };
}

function traceDescription(event: Record<string, string>) {
  return event.text || event.description || Object.entries(event)
    .filter(([key]) => key !== "time").map(([, value]) => value).join(" · ") || "物流详情未提供";
}

export function OrderDetailContent({ order }: { order: OrderCard }) {
  const activeOrderItem = order.items[0];
  const { events, latestKnown } = orderLogisticsEvents(order.logistics?.trace ?? []);
  const latest = latestKnown ? events[0] : null;
  const amountDiffers = order.total_amount !== order.pay_amount;
  return (
    <article className="order-box">
      <div className="order-head">
        <strong>#{order.id}</strong>
        <span data-status={order.status_label}>{order.status_label}</span>
      </div>
      <p>{activeOrderItem?.sku_name ?? "订单商品待确认"}
        {order.items.length > 1 && <small> 等 {order.items.length} 项商品</small>}
      </p>
      <dl className="order-facts">
        <div><dt>{amountDiffers ? "实付" : "金额"}</dt><dd>¥{order.pay_amount}</dd></div>
        <div className="order-created"><dt>下单时间</dt><dd>{formatDateTime(order.created_at)}</dd></div>
      </dl>

      <details className="order-section order-purchase">
        <summary className="order-section-heading">
          <Package size={16} aria-hidden="true" /><strong>订单详情</strong>
          <span className="order-disclosure-label"><span className="when-closed">查看商品与费用</span><span className="when-open">收起详情</span></span>
          <ChevronDown className="order-chevron" size={16} aria-hidden="true" />
        </summary>
        <div className="order-section-body">
          <h4>商品明细</h4>
          <div className="order-item-list">
            {order.items.map(item => (
              <div className="order-product" key={item.id}>
                <div><span>{item.sku_name}</span><small>{formatSkuSpecs(item.sku_specs)}</small></div>
                <b>¥{item.price} × {item.quantity}</b>
              </div>
            ))}
            {!order.items.length && <p className="muted">暂无商品明细</p>}
          </div>
          <h4>费用与时间</h4>
          <dl className="order-detail-facts">
            {amountDiffers && <div><dt>订单总额</dt><dd>¥{order.total_amount}</dd></div>}
            <div><dt>运费</dt><dd>¥{order.freight_amount}</dd></div>
            <div><dt>下单时间</dt><dd>{formatDateTime(order.created_at)}</dd></div>
            {order.pay_at && <div><dt>支付时间</dt><dd>{formatDateTime(order.pay_at)}</dd></div>}
            {order.delivery_at && <div><dt>发货时间</dt><dd>{formatDateTime(order.delivery_at)}</dd></div>}
          </dl>
        </div>
      </details>

      <section className="order-section order-shipping" aria-label="物流进度">
        <div className="order-section-heading"><Truck size={16} aria-hidden="true" /><strong>物流进度</strong></div>
        <div className="order-shipping-body">
          <div className="order-carrier">
            <span>{order.logistics?.express_company ?? "暂无快递信息"}</span>
            {order.logistics?.logistic_no && <span>运单号 {order.logistics.logistic_no}</span>}
          </div>
          {latest ? <div className="order-latest">
            <span className="order-latest-label">最新动态</span>
            <p>{traceDescription(latest)}</p>
            <time>{latest.time}</time>
          </div> : <p className="order-shipping-empty">{events.length
            ? "物流记录时间不完整，展开查看已有记录。" : "暂无物流轨迹，以后续查询为准。"}</p>}
          {!!events.length && <details className="order-trace-details">
            <summary>
              <span className="when-closed">查看完整轨迹 · {events.length} 条</span>
              <span className="when-open">收起轨迹</span>
              <ChevronDown className="order-chevron" size={14} aria-hidden="true" />
            </summary>
            <ol className="trace-list">
              {events.map((event, index) => <li className={`trace-row${latestKnown && index === 0 ? " trace-latest" : ""}`} key={`${order.id}-trace-${index}`}>
                <span>{traceDescription(event)}</span>
                <time>{event.time || "时间未提供"}</time>
              </li>)}
            </ol>
          </details>}
        </div>
      </section>
    </article>
  );
}

export function OrderCardView({ order, onOpen }: { order: OrderCard; onOpen?: () => void }) {
  const { events, latestKnown } = orderLogisticsEvents(order.logistics?.trace ?? []);
  const latest = latestKnown ? events[0] : null;
  const title = order.items[0]?.sku_name ?? "订单商品待确认";
  return <article className="order-box order-compact">
    <div className="order-head"><strong>#{order.id}</strong><span data-status={order.status_label}>{order.status_label}</span></div>
    <div className="order-compact-product"><span title={title}>{title}</span><b>¥{order.pay_amount}</b></div>
    <div className="order-compact-logistics"><Truck size={14} aria-hidden="true" /><span title={latest ? traceDescription(latest) : undefined}>
      {latest ? traceDescription(latest) : events.length ? "物流时间不完整，可查看已有记录" : "暂无物流轨迹"}
    </span></div>
    <div className="order-compact-footer"><small>{latest ? formatDateTime(latest.time) : `下单 ${formatDateTime(order.created_at)}`}</small>
      <button className="text-button" type="button" onClick={onOpen}>查看订单与物流</button>
    </div>
  </article>;
}

export function OrderSummaryCard({ order, index, full = false }: { order: OrderSummary; index: number; full?: boolean }) {
  const latest = order.latest_logistics_event;
  const latestText = latest ? displayValue(latest.text ?? latest.description ?? "") : "";
  const title = order.first_item_name ?? "订单商品待确认";
  return <article className={`order-box order-compact${full ? " order-summary-full" : ""}`}>
    <div className="order-head"><strong>{index + 1}. #{order.id}</strong><span data-status={order.status_label}>{order.status_label}</span></div>
    <div className="order-compact-product"><span title={title}>{title}</span><b>¥{order.pay_amount}</b></div>
    {latestText && <div className="order-compact-logistics"><Truck size={14} aria-hidden="true" /><span title={latestText}>{latestText}</span></div>}
    <div className="order-compact-footer"><small>共 {order.item_count} 件 · 下单 {formatDateTime(order.created_at)}</small></div>
    {full && <>
      {(order.express_company || order.logistic_no) && <small>{order.express_company} {order.logistic_no}</small>}
      {latest && <small>{Object.values(latest).map(displayValue).join(" · ")}</small>}
    </>}
  </article>;
}

// Order snapshots have no reliable category; use generic labels for category-dependent fields.
export function formatSkuSpecs(specs: Record<string, unknown> | null | undefined) {
  const rows = formatSpecs(specs);
  return rows.length ? rows.map(({ label, value }) => `${label}：${value}`).join(" · ") : "规格未标注";
}
