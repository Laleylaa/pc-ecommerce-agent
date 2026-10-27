import { AlertTriangle, ArrowUp, Bot, CircleStop, Headset, Keyboard, PanelLeftOpen, RefreshCcw, Truck, BookOpenText } from "lucide-react";
import { FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { formatClock } from "./common";
import { MarkdownContent } from "./MarkdownContent";
import { ProductCardRow } from "./ProductInlineCard";
import { MessageResults } from "./MessageResults";
import type { ChatMessage, ProductCard, RequestError, SuggestedAction } from "../types";

type ChatPanelProps = {
  messages: ChatMessage[]; input: string; loading: boolean; suggestedActions: SuggestedAction[];
  error: RequestError | null; title?: string; sidebarOpen: boolean;
  onToggleSidebar: () => void; onInputChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void; onCancel: () => void; onRetry: () => void;
  onSuggestedAction: (action: SuggestedAction) => void; onProductClick: (product: ProductCard) => void;
  onViewProducts: (products: ProductCard[]) => void;
  onAsk: (text: string) => void; onHandoff: () => void;
};
const starters = [
  { icon: Keyboard, title: "挑选合适的外设", hint: "从预算、品类和规格开始", text: "推荐几款 500 元以内的无线机械键盘" },
  { icon: Truck, title: "看看订单到哪了", hint: "查询订单与已有物流信息", text: "查询我最近一笔订单和物流信息" },
  { icon: BookOpenText, title: "了解售后与使用", hint: "店铺政策、参数和使用问题", text: "本店退货需要满足什么条件？" }
];
export function ChatPanel({ messages, input, loading, suggestedActions, error, title, sidebarOpen,
  onToggleSidebar, onInputChange, onSubmit, onCancel, onRetry, onSuggestedAction, onProductClick, onViewProducts, onAsk, onHandoff }: ChatPanelProps) {
  const messagesRef = useRef<HTMLElement | null>(null);
  const previousLast = useRef<ChatMessage | undefined>(undefined);
  const [unreadReply, setUnreadReply] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const nearBottom = useRef(true);
  const welcome = messages.length === 1 && messages[0].id === "hello";
  const length = Array.from(input).length;
  function revealMessage(id: string) {
    const container = messagesRef.current;
    const row = Array.from(container?.querySelectorAll<HTMLElement>("[data-message-id]") ?? [])
      .find(element => element.dataset.messageId === id);
    if (!container || !row) return;
    const viewport = container.getBoundingClientRect();
    const bounds = row.getBoundingClientRect();
    // Scroll only the conversation pane. Long replies start at their heading;
    // short replies fit in full. Never scroll to the end of a tall result group.
    const top = container.scrollTop + bounds.top - viewport.top;
    container.scrollTop = bounds.height > container.clientHeight - 32
      ? Math.max(0, top - 16) : Math.max(0, top + bounds.height - container.clientHeight + 24);
    nearBottom.current = container.scrollHeight - container.scrollTop - container.clientHeight < 160;
  }
  useLayoutEffect(() => {
    const last = messages.at(-1);
    const previous = previousLast.current;
    previousLast.current = last;
    if (!last || (previous?.id === last.id && previous.status === last.status)) return;
    const replaced = !previous || !messages.some(message => message.id === previous.id);
    if (replaced || nearBottom.current || last.role === "user") {
      revealMessage(last.id);
      setUnreadReply(null);
    } else if (last.role === "assistant" && last.status !== "pending") {
      setUnreadReply(last.id);
    }
  }, [messages]);
  useEffect(() => {
    if (inputRef.current) { inputRef.current.style.height = "auto"; inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 160)}px`; }
  }, [input]);
  return <main className="chat-panel">
    <header className="topbar"><div className="chat-title">
      <button className="icon-button sidebar-open-button" type="button" aria-label={sidebarOpen ? "收起历史咨询" : "展开历史咨询"}
        aria-expanded={sidebarOpen} onClick={onToggleSidebar}><PanelLeftOpen size={20} /></button>
      <div><h1>{title || "小帕瓦"}</h1><span>外设选购与服务助手</span></div></div>
      <button type="button" className="text-button" onClick={onHandoff} disabled={loading}><Headset size={17} /><span>人工诉求</span></button>
    </header>
    <section ref={messagesRef} className={`messages${welcome ? " is-welcome" : ""}`} aria-label="咨询内容" onScroll={event => {
      const node = event.currentTarget; nearBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 160;
      if (nearBottom.current) setUnreadReply(null);
    }}>
      <div className="message-column">
        {welcome ? <div className="welcome"><span className="welcome-mark"><Bot size={32} strokeWidth={1.5} /></span>
          <span className="eyebrow">你的外设咨询伙伴</span><h2>好用的外设，从聊聊开始。</h2>
          <p>选商品、比参数、查订单。告诉小帕瓦你需要什么。</p>
          <div className="starter-grid">{starters.map(item => <button key={item.title} type="button" onClick={() => { onInputChange(item.text); inputRef.current?.focus(); }}>
            <item.icon size={22} strokeWidth={1.5} /><strong>{item.title}</strong><small>{item.hint}</small><span>试着问问 ↗</span>
          </button>)}</div></div> : messages.map(message => <MessageRow key={message.id} message={message} onProductClick={onProductClick} onViewProducts={onViewProducts} onAsk={text => { onAsk(text); inputRef.current?.focus(); }} loading={loading} />)}
        {error && <div className="error-card" role="alert"><AlertTriangle size={18} /><div><strong>这次请求未完成</strong><p>{error.message}</p></div>
          {error.retryable && error.request && <button className="text-button" onClick={onRetry} disabled={loading}><RefreshCcw size={15} />重试</button>}</div>}
      </div>
    </section>
    <div className="composer-region">
      {unreadReply && <button type="button" className="new-reply-button" onClick={() => { revealMessage(unreadReply); setUnreadReply(null); }}>
        有新回复 · 点击查看</button>}
      {!!suggestedActions.length && <div className="suggestion-strip">{suggestedActions.map((action, index) =>
        <button key={`${action.label}-${index}`} type="button" disabled={loading} onClick={() => onSuggestedAction(action)}>{action.label}</button>)}</div>}
      <form className="composer" onSubmit={event => { if (!input.trim() || length > 1000) { event.preventDefault(); return; } nearBottom.current = true; onSubmit(event); }}>
        <textarea ref={inputRef} rows={1} value={input} onChange={event => onInputChange(event.target.value)} aria-label="输入咨询内容"
          placeholder="描述你想找的外设，或继续追问…" disabled={loading} onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
              event.preventDefault(); event.currentTarget.form?.requestSubmit();
            }
          }} />
        {loading ? <button type="button" className="send-button" aria-label="停止等待" onClick={onCancel}><CircleStop size={20} /></button>
          : <button type="submit" className="send-button" disabled={!input.trim() || length > 1000} aria-label="发送"><ArrowUp size={21} /></button>}
      </form>
      <div className="composer-note"><span>{loading ? "正在处理你的请求，可以停止等待。" : "商品价格、库存与物流以最新查询为准"}</span><span className={length > 1000 ? "input-limit" : ""}>{length > 0 ? `${length} / 1000` : "Enter 发送 · Shift + Enter 换行"}</span></div>
    </div>
  </main>;
}
function MessageRow({ message, onProductClick, onViewProducts, onAsk, loading }: {
  message: ChatMessage; onProductClick: (product: ProductCard) => void; onAsk: (text: string) => void; loading: boolean;
  onViewProducts: (products: ProductCard[]) => void;
}) {
  return <article data-message-id={message.id} className={`message ${message.role} ${message.status ?? ""}`}>
    {message.role === "assistant" && <div className="assistant-label"><span className="avatar"><Bot size={17} /></span><strong>小帕瓦</strong><span>{formatClock(message.createdAt)}</span></div>}
    <div className="bubble-stack">
      {message.role === "assistant" ? <>
        {message.status === "pending" ? <div className="pending-message" role="status"><span className="thinking-dots">•••</span>正在处理你的请求</div> : <MarkdownContent>{message.content}</MarkdownContent>}
        {!!message.products?.length && <ProductCardRow products={message.products} onProductClick={onProductClick} onViewAll={onViewProducts} />}
        <MessageResults message={message} onAsk={onAsk} disabled={loading} />
      </> : <p>{message.content}</p>}
      {message.status === "failed" && <small className="input-limit">发送失败</small>}
      {message.status === "cancelled" && <small className="muted">已停止等待</small>}
    </div>
  </article>;
}
