import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleSlash,
  CircleStop,
  Headset,
  Loader2,
  RefreshCcw,
  Send,
  UserRound
} from "lucide-react";
import { FormEvent, useEffect, useRef } from "react";
import { BoundaryBadge } from "./Boundary";
import { formatClock } from "./common";
import { MarkdownContent } from "./MarkdownContent";
import { ProductCardRow } from "./ProductInlineCard";
import type {
  BoundaryClassification,
  ChatMessage,
  ProductCard,
  RequestError,
  ResponseStatus,
  SuggestedAction
} from "../types";

type ChatPanelProps = {
  conversationId?: number;
  messages: ChatMessage[];
  input: string;
  loading: boolean;
  responseStatus: ResponseStatus;
  boundary: BoundaryClassification | null;
  suggestedActions: SuggestedAction[];
  error: RequestError | null;
  onInputChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onRetry: () => void;
  onSuggestedAction: (action: SuggestedAction) => void;
  onProductClick?: (product: ProductCard) => void;
};

export function ChatPanel({
  conversationId,
  messages,
  input,
  loading,
  responseStatus,
  boundary,
  suggestedActions,
  error,
  onInputChange,
  onSubmit,
  onCancel,
  onRetry,
  onSuggestedAction,
  onProductClick
}: ChatPanelProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, loading, error]);

  return (
    <main className="chat-panel">
      <header className="topbar">
        <div>
          <h1>客服会话</h1>
          <span>{conversationId ? `#${conversationId}` : "ready"}</span>
        </div>
        <div className="status-stack">
          {boundary && <BoundaryBadge boundary={boundary} />}
          <StatusPill status={responseStatus} loading={loading} />
        </div>
      </header>

      <section className="messages" aria-live="polite">
        {messages.map((message) => (
          <MessageRow key={message.id} message={message} onProductClick={onProductClick} />
        ))}
        {loading && !messages.some((message) => message.status === "pending") && (
          <article className="message assistant pending">
            <span className="avatar">
              <Bot size={17} />
            </span>
            <div className="bubble-stack">
              <p>
                <Loader2 size={15} className="spin inline-icon" />
                正在处理
              </p>
            </div>
          </article>
        )}
        {error && <ErrorBanner error={error} onRetry={onRetry} />}
        <div ref={endRef} />
      </section>

      {suggestedActions.length > 0 && (
        <div className="suggestion-strip">
          {suggestedActions.map((action, index) => (
            <button
              key={`${action.label}-${index}`}
              type="button"
              disabled={loading}
              onClick={() => onSuggestedAction(action)}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}

      <form className="composer" onSubmit={onSubmit}>
        <input
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          placeholder="输入预算、用途、订单号或售后诉求"
          disabled={loading}
        />
        <button type="submit" disabled={loading || !input.trim()} title="发送">
          {loading ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
        </button>
        <button type="button" disabled={!loading} title="取消" onClick={onCancel}>
          <CircleStop size={18} />
        </button>
      </form>
    </main>
  );
}

function MessageRow({ message, onProductClick }: { message: ChatMessage; onProductClick?: (product: ProductCard) => void }) {
  const metaParts = messageMeta(message);
  const hasProducts =
    message.role === "assistant" && message.products !== undefined && message.products.length > 0;
  const bubbleContent =
    message.content ||
    (message.status === "pending" ? message.streamStage || "正在处理" : message.content);
  return (
    <article
      className={`message ${message.role} ${hasProducts ? "has-products" : ""} ${
        message.status === "failed" ? "failed" : ""
      } ${
        message.status === "pending" ? "pending" : ""
      }`}
    >
      <span className="avatar">
        {message.role === "assistant" ? <Bot size={17} /> : <UserRound size={17} />}
      </span>
      <div className="bubble-stack">
        {message.boundary && <BoundaryBadge boundary={message.boundary} compact />}
        {message.role === "assistant" ? (
          <MarkdownContent>
            {bubbleContent}
          </MarkdownContent>
        ) : (
          <p>
            {message.status === "pending" && !message.content && (
              <Loader2 size={15} className="spin inline-icon" />
            )}
            {bubbleContent}
          </p>
        )}
        {message.role === "assistant" && message.products && message.products.length > 0 && (
          <ProductCardRow products={message.products} onProductClick={onProductClick} />
        )}
        {metaParts.length > 0 && (
          <div className="message-meta">
            {metaParts.map((part) => (
              <span key={part}>{part}</span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

function ErrorBanner({ error, onRetry }: { error: RequestError; onRetry: () => void }) {
  return (
    <div className="error-card">
      <div className="error-main">
        <AlertTriangle size={18} />
        <div>
          <strong>{error.status ? `HTTP ${error.status}` : "请求未完成"}</strong>
          <p>{error.message}</p>
        </div>
      </div>
      {error.retryable && error.request && (
        <button type="button" className="ghost-button danger" onClick={onRetry}>
          <RefreshCcw size={15} />
          重试
        </button>
      )}
    </div>
  );
}

function StatusPill({ status, loading }: { status: ResponseStatus; loading: boolean }) {
  const meta = statusMeta(loading ? "loading" : status);
  return (
    <span className={`status-pill ${meta.className}`}>
      {meta.icon}
      {meta.label}
    </span>
  );
}

function statusMeta(status: ResponseStatus | "loading") {
  if (status === "loading") {
    return {
      className: "busy",
      icon: <Loader2 size={14} className="spin" />,
      label: "处理中"
    };
  }
  if (status === "streaming") {
    return {
      className: "busy",
      icon: <Loader2 size={14} className="spin" />,
      label: "生成中"
    };
  }
  if (status === "handoff") {
    return {
      className: "handoff",
      icon: <Headset size={14} />,
      label: "待人工"
    };
  }
  if (status === "blocked") {
    return {
      className: "blocked",
      icon: <CircleSlash size={14} />,
      label: "已拒答"
    };
  }
  if (status === "error") {
    return {
      className: "error",
      icon: <AlertTriangle size={14} />,
      label: "错误"
    };
  }
  if (status === "cancelled") {
    return {
      className: "blocked",
      icon: <CircleStop size={14} />,
      label: "已取消"
    };
  }
  if (status === "success") {
    return {
      className: "success",
      icon: <CheckCircle2 size={14} />,
      label: "已回答"
    };
  }
  return {
    className: "ready",
    icon: <CheckCircle2 size={14} />,
    label: "就绪"
  };
}

function messageMeta(message: ChatMessage) {
  const parts = [formatClock(message.createdAt)].filter(Boolean);
  if (message.status === "failed") parts.push("发送失败");
  if (message.status === "pending") parts.push(message.streamStage ?? "处理中");
  if (message.status === "streaming") parts.push(message.streamStage ?? "生成中");
  if (message.status === "cancelled") parts.push("已取消");
  if (message.intent) parts.push(message.intent);
  if (message.evidenceCount) parts.push(`${message.evidenceCount} 条依据`);
  if (message.productCount) parts.push(`${message.productCount} 个商品`);
  if (message.orderId) parts.push(`订单 #${message.orderId}`);
  return parts;
}
