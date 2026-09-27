import {
  BookOpenText,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ClipboardList,
  Headset,
  History,
  Loader2,
  MessageSquareText,
  PackageSearch,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Truck
} from "lucide-react";
import { useState } from "react";
import { BoundaryBadge, BoundaryStatusCard, BoundaryStatusBar } from "./Boundary";
import { EmptyState, displayValue, formatDateTime } from "./common";
import { getCategoryIcon } from "../utils/category-icon";
import type {
  BoundaryClassification,
  ConversationTurn,
  EvidenceItem,
  HandoffRequest,
  HandoffRequestType,
  HandoffNotice,
  MemoryItem,
  OrderCard,
  OrderQueryMeta,
  OrderSummary,
  ProductCard
} from "../types";

const handoffStatusLabels: Record<string, string> = {
  pending: "待人工确认",
  acknowledged: "已确认",
  resolved: "已解决"
};

const handoffSafetyMessage =
  "当前系统不会自动办理退款、退货、维修或订单修改等业务操作，请求已记录待人工确认";

type ContextPanelProps = {
  boundary: BoundaryClassification | null;
  evidence: EvidenceItem[];
  products: ProductCard[];
  order: OrderCard | null;
  orders: OrderSummary[];
  orderQuery: OrderQueryMeta | null;
  turns: ConversationTurn[];
  memories: MemoryItem[];
  memoriesLoading: boolean;
  memoryError: string | null;
  forgettingMemoryIds: ReadonlySet<number>;
  handoffNotice: HandoffNotice | null;
  ticketType: HandoffRequestType;
  ticketReason: string;
  loading: boolean;
  handoffQueryId: string;
  handoffQueryLoading: boolean;
  handoffQueryError: string | null;
  handoffQueryResult: HandoffRequest | null;
  skuCount: number;
  orderCount: number;
  evidenceCount: number;
  highlightedProductId?: number | null;
  mobileTab?: "chat" | "products" | "details";
  onTicketTypeChange: (value: string) => void;
  onTicketReasonChange: (value: string) => void;
  onRequestHandoff: () => void;
  onAcknowledgeHandoff: () => void;
  onHandoffQueryIdChange: (value: string) => void;
  onQueryHandoffRequest: () => void;
  onForgetMemory: (memoryId: number) => void;
};

export function ContextPanel({
  boundary,
  evidence,
  products,
  order,
  orders,
  orderQuery,
  turns,
  memories,
  memoriesLoading,
  memoryError,
  forgettingMemoryIds,
  handoffNotice,
  ticketType,
  ticketReason,
  loading,
  handoffQueryId,
  handoffQueryLoading,
  handoffQueryError,
  handoffQueryResult,
  skuCount,
  orderCount,
  evidenceCount,
  highlightedProductId,
  mobileTab,
  onTicketTypeChange,
  onTicketReasonChange,
  onRequestHandoff,
  onAcknowledgeHandoff,
  onHandoffQueryIdChange,
  onQueryHandoffRequest,
  onForgetMemory
}: ContextPanelProps) {
  const showAfterSales =
    boundary?.classification === "human_handoff_required" || handoffNotice !== null;

  // On mobile, filter sections by active tab
  const isMobile = mobileTab !== undefined;
  const showProducts = !isMobile || mobileTab === "products";
  const showDetails = !isMobile || mobileTab === "details";
  const mobileVisible = isMobile && mobileTab !== "chat";

  const hasData =
    products.length > 0 ||
    order !== null ||
    orders.length > 0 ||
    evidence.length > 0 ||
    turns.length > 0 ||
    memories.length > 0;

  return (
    <aside className={`context-panel${mobileVisible ? " mobile-visible" : ""}`}>
      {/* L1: Status */}
      {showProducts && (
        <div className="metrics-strip">
          <Metric icon={<Boxes size={14} />} label="SKU" value={skuCount} />
          <Metric icon={<Truck size={14} />} label="订单" value={orderCount} />
          <Metric icon={<BookOpenText size={14} />} label="依据" value={evidenceCount} />
        </div>
      )}

      {showDetails && (
        <section className="panel-section">
          <div className="section-title">
            <ShieldCheck size={14} />
            <h2>状态</h2>
          </div>
          {boundary ? (
            <BoundaryStatusBar boundary={boundary} />
          ) : (
            <EmptyState text="等待请求" />
          )}
        </section>
      )}

      {showDetails && (
        <section className="panel-section">
          <div className="section-title">
            <Sparkles size={14} />
            <h2>已记住偏好</h2>
            {memories.length > 0 && <span className="section-count">{memories.length}</span>}
          </div>
          {memoriesLoading ? (
            <div className="memory-state">
              <Loader2 className="spin" size={14} />
              <span>正在加载偏好</span>
            </div>
          ) : memoryError ? (
            <div className="memory-state memory-error" role="alert">
              {memoryError}
            </div>
          ) : memories.length > 0 ? (
            <div className="memory-list">
              {memories.map((memory) => {
                const pending = forgettingMemoryIds.has(memory.id);
                return (
                  <article className="memory-item" key={memory.id}>
                    <div>
                      <strong>{memory.display_value}</strong>
                      <small>{formatDateTime(memory.updated_at)}</small>
                    </div>
                    <button
                      type="button"
                      onClick={() => onForgetMemory(memory.id)}
                      disabled={pending}
                      aria-label={`忘记偏好：${memory.display_value}`}
                    >
                      {pending && <Loader2 className="spin" size={12} />}
                      {pending ? "处理中" : "忘记"}
                    </button>
                  </article>
                );
              })}
            </div>
          ) : (
            <EmptyState text="还没有已记住的偏好" />
          )}
        </section>
      )}

      {/* Welcome guide when no data yet */}
      {showDetails && !hasData && !boundary && (
        <section className="panel-section panel-section-hero">
          <div className="hero-icon">
            <MessageSquareText size={28} />
          </div>
          <h3 className="hero-title">工作台详情</h3>
          <p className="hero-desc">
            在左侧发送消息，这里会实时展示检索到的商品、订单和知识依据。
          </p>
          <div className="hero-hints">
            <div className="hero-hint">
              <PackageSearch size={14} />
              <span>推荐商品与规格对比</span>
            </div>
            <div className="hero-hint">
              <Truck size={14} />
              <span>订单与物流查询</span>
            </div>
            <div className="hero-hint">
              <BookOpenText size={14} />
              <span>售后政策与知识依据</span>
            </div>
          </div>
        </section>
      )}

      {/* L2: Primary */}
      {showProducts && (
        <section className="panel-section">
          <div className="section-title">
            <PackageSearch size={14} />
            <h2>商品</h2>
            {products.length > 0 && <span className="section-count">{products.length}</span>}
          </div>
          {products.length > 0 ? (
            <div className="product-list">
              {products.map((product) => (
                <ProductCardView key={product.sku_id} product={product} highlighted={product.sku_id === highlightedProductId} />
              ))}
            </div>
          ) : (
            <EmptyState text="询问商品推荐后展示" />
          )}
        </section>
      )}

      {showDetails && (
        <section className="panel-section">
          <div className="section-title">
            <Truck size={14} />
            <h2>订单</h2>
          </div>
          {order ? (
            <OrderCardView order={order} />
          ) : orders.length > 0 ? (
            <div className="order-list">
              {orderQuery && (
                <small>
                  共 {orderQuery.total_match_count} 个，本次显示 {orderQuery.returned_count} 个
                  {!orderQuery.is_exhaustive ? "，还有更多" : ""}
                </small>
              )}
              {orders.map((item, index) => (
                <OrderSummaryCard key={item.id} order={item} index={index} />
              ))}
            </div>
          ) : (
            <EmptyState
              text={
                orderQuery?.query_mode === "count"
                  ? `当前共有 ${orderQuery.total_match_count} 个订单`
                  : orderQuery?.query_mode === "page" &&
                      orderQuery.returned_count === 0 &&
                      orderQuery.total_match_count > 0
                    ? `已显示完全部 ${orderQuery.total_match_count} 个订单`
                  : "查询订单后展示"
              }
            />
          )}
        </section>
      )}

      {/* L3: Details */}
      {showDetails && (
        <section className="panel-section">
          <div className="section-title">
            <BookOpenText size={14} />
            <h2>依据</h2>
            {evidence.length > 0 && <span className="section-count">{evidence.length}</span>}
          </div>
          {evidence.length > 0 ? (
            <div className="evidence-list">
              {evidence.map((item) => (
                <EvidenceCard key={`${item.source_type}-${item.source_id}`} evidence={item} />
              ))}
            </div>
          ) : (
            <EmptyState text="回答带依据时展示" />
          )}
        </section>
      )}

      {showDetails && (
        <section className="panel-section">
          <div className="section-title">
            <History size={14} />
            <h2>上下文</h2>
            {turns.length > 0 && <span className="section-count">{turns.length}</span>}
          </div>
          {turns.length > 0 ? (
            <ConversationTimeline turns={turns} />
          ) : (
            <EmptyState text="多轮对话后展示" />
          )}
        </section>
      )}

      {showDetails && (boundary?.classification === "human_handoff_required" || handoffNotice) && (
        <section className="panel-section">
          <div className="section-title">
            <Headset size={14} />
            <h2>接管</h2>
          </div>
          <HandoffPanel
            boundary={boundary}
            notice={handoffNotice}
            order={order}
            loading={loading}
            queryId={handoffQueryId}
            queryLoading={handoffQueryLoading}
            queryError={handoffQueryError}
            queryResult={handoffQueryResult}
            onAcknowledge={onAcknowledgeHandoff}
            onQueryIdChange={onHandoffQueryIdChange}
            onQuery={onQueryHandoffRequest}
          />
        </section>
      )}

      {showDetails && showAfterSales && (
        <section className="panel-section">
          <div className="section-title">
            <RotateCcw size={14} />
            <h2>售后</h2>
          </div>
          <div className="ticket-form">
            <select
              aria-label="售后类型"
              value={ticketType}
              onChange={(event) => onTicketTypeChange(event.target.value)}
              disabled={loading}
            >
              <option value="return">退货</option>
              <option value="refund">退款</option>
              <option value="repair">维修</option>
              <option value="order_change">订单修改</option>
              <option value="other">其他</option>
            </select>
            <input
              aria-label="售后原因"
              value={ticketReason}
              onChange={(event) => onTicketReasonChange(event.target.value)}
              disabled={loading}
              placeholder="描述售后原因"
            />
            <button type="button" onClick={onRequestHandoff} disabled={loading || !ticketReason.trim()}>
              记录人工确认诉求
            </button>
          </div>
        </section>
      )}
    </aside>
  );
}

function Metric({
  icon,
  label,
  value
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="metric">
      {icon}
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
    </div>
  );
}

function HandoffPanel({
  boundary,
  notice,
  order,
  loading,
  queryId,
  queryLoading,
  queryError,
  queryResult,
  onAcknowledge,
  onQueryIdChange,
  onQuery
}: {
  boundary: BoundaryClassification | null;
  notice: HandoffNotice | null;
  order: OrderCard | null;
  loading: boolean;
  queryId: string;
  queryLoading: boolean;
  queryError: string | null;
  queryResult: HandoffRequest | null;
  onAcknowledge: () => void;
  onQueryIdChange: (value: string) => void;
  onQuery: () => void;
}) {
  const activeBoundary =
    boundary?.classification === "human_handoff_required" ? boundary : null;
  const requested = notice?.requested ?? false;
  const orderId = notice?.orderId ?? order?.id;
  const status = queryResult?.status ?? notice?.status;
  const statusLabel = status ? handoffStatusLabels[status] : requested ? "已记录" : "待记录";
  const requestId = queryResult?.id ?? notice?.requestId;

  return (
    <article className="handoff-card">
      <div className="handoff-card-head">
        <div>
          <Headset size={16} />
          <strong>人工接管</strong>
        </div>
        <span>{requestId ? `#${requestId} · ${statusLabel}` : statusLabel}</span>
      </div>
      <p>{requested ? handoffSafetyMessage : activeBoundary?.display_message ?? notice?.reason}</p>
      <ul className="handoff-list">
        <li>{orderId ? `订单 #${orderId}` : "订单待确认"}</li>
        <li>{notice?.reason ?? activeBoundary?.reason ?? "需要人工确认"}</li>
        <li>{notice ? formatDateTime(notice.updatedAt) : "刚刚"}</li>
      </ul>
      {notice?.message && <p className="handoff-message">{notice.message}</p>}
      <button type="button" onClick={onAcknowledge} disabled={loading || requested}>
        <CheckCircle2 size={15} />
        {requested ? "已记录" : "记录人工确认诉求"}
      </button>
      <div className="handoff-query">
        <div className="handoff-query-row">
          <input
            aria-label="人工接管请求编号"
            inputMode="numeric"
            value={queryId}
            onChange={(event) => onQueryIdChange(event.target.value)}
            placeholder="输入请求编号"
          />
          <button type="button" onClick={onQuery} disabled={queryLoading}>
            <Search size={15} />
            查询状态
          </button>
        </div>
        {queryError && <p className="handoff-query-error">{queryError}</p>}
        {queryResult && (
          <p className="handoff-query-result">
            查询结果：#{queryResult.id} · {handoffStatusLabels[queryResult.status]} ·{" "}
            {formatDateTime(queryResult.updated_at)}
          </p>
        )}
      </div>
    </article>
  );
}

function ConversationTimeline({ turns }: { turns: ConversationTurn[] }) {
  const recentTurns = turns.slice(-5).reverse();

  return (
    <div className="turn-list">
      {recentTurns.map((turn, index) => (
        <article className="turn-card" key={turn.id}>
          <header>
            <strong>#{turns.length - index}</strong>
            <BoundaryBadge boundary={turn.boundary} compact />
          </header>
          <p>{turn.userMessage}</p>
          <div className="turn-facts">
            <span>{turn.intent}</span>
            <span>{turn.evidenceCount} 依据</span>
            <span>{turn.productCount} 商品</span>
            {turn.orderId && <span>订单 #{turn.orderId}</span>}
          </div>
        </article>
      ))}
    </div>
  );
}

function EvidenceCard({ evidence }: { evidence: EvidenceItem }) {
  const metadata = Object.entries(evidence.metadata).slice(0, 3);
  return (
    <article className="evidence-card">
      <div className="evidence-card-head">
        <strong>{evidence.title}</strong>
        <span>{evidence.document_type}</span>
      </div>
      <p>{evidence.snippet}</p>
      {metadata.length > 0 && (
        <div className="metadata-list">
          {metadata.map(([key, value]) => (
            <span key={key}>
              {key}: {displayValue(value)}
            </span>
          ))}
        </div>
      )}
      <small>
        #{evidence.source_id}
        {typeof evidence.score === "number" ? ` · score ${evidence.score.toFixed(2)}` : ""}
      </small>
    </article>
  );
}

function ProductCardView({ product, highlighted }: { product: ProductCard; highlighted: boolean }) {
  const IconComponent = getCategoryIcon(product.category);
  const isSeriesProduct = product.entity_scope === "spu" || product.ranking_scope === "spu";
  const displayTitle = isSeriesProduct && product.spu_title ? product.spu_title : product.title;
  const specLine = isSeriesProduct
    ? product.series_sku_count
      ? `共 ${product.series_sku_count} 个在售版本`
      : "商品系列"
    : Object.entries(product.specs)
        .slice(0, 4)
        .map(([key, value]) => `${key}: ${value}`)
        .join(" · ");
  const priceLabel =
    isSeriesProduct && product.series_min_price
      ? product.series_max_price &&
        product.series_max_price !== product.series_min_price
        ? `¥${product.series_min_price}–${product.series_max_price}`
        : `¥${product.series_min_price}`
      : `¥${product.price}`;
  const stockLabel =
    isSeriesProduct && product.series_total_stock !== null &&
    product.series_total_stock !== undefined
      ? `系列库存 ${product.series_total_stock}`
      : `库存 ${product.stock}`;
  return (
    <article className={`product-card${highlighted ? " highlighted" : ""}`}>
      <div className="thumb">
        {product.image_url ? (
          <img src={product.image_url} alt={displayTitle} loading="lazy" />
        ) : (
          <IconComponent size={24} />
        )}
      </div>
      <div>
        <h3>{displayTitle}</h3>
        <p>
          {product.brand} · {product.category}
        </p>
        <small>{specLine || "规格未标注"}</small>
      </div>
      <div className="product-foot">
        <strong>{priceLabel}</strong>
        <span>{stockLabel}</span>
      </div>
    </article>
  );
}

function OrderCardView({ order }: { order: OrderCard }) {
  const [expanded, setExpanded] = useState(false);
  const activeOrderItem = order.items[0];
  const lastTrace = order.logistics?.trace.at(-1);
  const amountDiffers = order.total_amount !== order.pay_amount;
  return (
    <article className="order-box">
      <button
        type="button"
        className="order-summary-button"
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="order-head">
          <strong>#{order.id}</strong>
          <span>{order.status_label}</span>
        </span>
        {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </button>
      <p>{activeOrderItem?.sku_name ?? "订单商品待确认"}</p>
      <dl className="order-facts">
        <div>
          <dt>{amountDiffers ? "实付" : "金额"}</dt>
          <dd>¥{order.pay_amount}</dd>
        </div>
        {amountDiffers && (
          <div>
            <dt>订单总额</dt>
            <dd>¥{order.total_amount}</dd>
          </div>
        )}
        <div>
          <dt>下单</dt>
          <dd>{formatDateTime(order.created_at)}</dd>
        </div>
      </dl>
      <div className="order-logistics">
        <ClipboardList size={15} />
        <span>
          {order.logistics?.express_company ?? "待分配快递"} {order.logistics?.logistic_no ?? ""}
        </span>
      </div>
      {lastTrace && <small>{Object.values(lastTrace).slice(0, 2).join(" · ")}</small>}
      {expanded && (
        <div className="order-detail">
          <div className="order-detail-section">
            <strong>订单信息</strong>
            <div className="order-item-list">
              <div className="order-item-row">
                <span>运费</span>
                <b>¥{order.freight_amount}</b>
              </div>
              {order.pay_at && (
                <div className="order-item-row">
                  <span>支付时间</span>
                  <b>{formatDateTime(order.pay_at)}</b>
                </div>
              )}
              {order.delivery_at && (
                <div className="order-item-row">
                  <span>发货时间</span>
                  <b>{formatDateTime(order.delivery_at)}</b>
                </div>
              )}
            </div>
          </div>
          <div className="order-detail-section">
            <strong>商品明细</strong>
            <div className="order-item-list">
              {order.items.map((item) => (
                <div className="order-item-row" key={item.id}>
                  <span>{item.sku_name}</span>
                  <small>{formatSkuSpecs(item.sku_specs)}</small>
                  <b>
                    ¥{item.price} × {item.quantity}
                  </b>
                </div>
              ))}
            </div>
          </div>
          <div className="order-detail-section">
            <strong>物流轨迹</strong>
            <div className="trace-list">
              {order.logistics?.trace.length ? (
                order.logistics.trace.map((trace, index) => (
                  <div className="trace-row" key={`${order.id}-trace-${index}`}>
                    {Object.values(trace)
                      .slice(0, 3)
                      .map((value) => (
                        <span key={value}>{value}</span>
                      ))}
                  </div>
                ))
              ) : (
                <span className="trace-empty">暂无轨迹</span>
              )}
            </div>
          </div>
        </div>
      )}
    </article>
  );
}

function OrderSummaryCard({ order, index }: { order: OrderSummary; index: number }) {
  const latestTrace = order.latest_logistics_event
    ? Object.values(order.latest_logistics_event).slice(0, 2).join(" · ")
    : null;
  return (
    <article className="order-box">
      <span className="order-head">
        <strong>
          {index + 1}. #{order.id}
        </strong>
        <span>{order.status_label}</span>
      </span>
      <p>{order.first_item_name ?? "订单商品待确认"}</p>
      <dl className="order-facts">
        <div>
          <dt>金额</dt>
          <dd>¥{order.pay_amount}</dd>
        </div>
        <div>
          <dt>下单</dt>
          <dd>{formatDateTime(order.created_at)}</dd>
        </div>
      </dl>
      <small>
        共 {order.item_count} 件商品
        {order.logistic_no
          ? ` · ${order.express_company ?? "快递"} ${order.logistic_no}`
          : ""}
      </small>
      {latestTrace && <small>{latestTrace}</small>}
    </article>
  );
}

function formatSkuSpecs(specs: Record<string, unknown> | null | undefined) {
  if (!specs || !Object.keys(specs).length) return "规格未标注";
  return Object.entries(specs)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${displayValue(value)}`)
    .join(" · ");
}
