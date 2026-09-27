import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import {
  ApiError,
  clearAuthSession,
  createHandoffRequest,
  deleteConversation as deleteConversationApi,
  forgetMemory,
  getConversation,
  getHandoffRequest,
  listConversations,
  listMemories,
  login,
  logout,
  restoreSession,
  sendChat
} from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { ContextPanel } from "./components/ContextPanel";
import { LoginPage } from "./components/LoginPage";
import { MobileTabBar } from "./components/MobileTabBar";
import type { MobileTab } from "./components/MobileTabBar";
import { Sidebar } from "./components/Sidebar";
import type {
  AuthSession,
  BoundaryClassification,
  ChatResponse,
  ChatMessage,
  ConversationDetail,
  ConversationSummary,
  ConversationTurn,
  EvidenceItem,
  HandoffRequest,
  HandoffRequestType,
  HandoffNotice,
  MemoryItem,
  OperatorProfile,
  OrderCard,
  OrderQueryMeta,
  OrderSummary,
  PendingRequest,
  ProductCard,
  RequestError,
  ResponseStatus,
  SuggestedAction
} from "./types";

const ticketTypeLabels: Record<HandoffRequestType, string> = {
  return: "退货",
  refund: "退款",
  repair: "维修",
  order_change: "订单修改",
  other: "其他"
};

type SubmitOptions = {
  appendUser?: boolean;
  conversationId?: number;
  messageId?: string;
};

export default function App() {
  const requestAbortRef = useRef<AbortController | null>(null);
  const memoryRequestVersionRef = useRef(0);
  const workspaceVersionRef = useRef(0);
  const conversationListRequestVersionRef = useRef(0);
  const conversationDetailRequestVersionRef = useRef(0);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authStatus, setAuthStatus] = useState<"restoring" | "ready" | "submitting">(
    "restoring"
  );
  const [authError, setAuthError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [memoriesLoading, setMemoriesLoading] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [forgettingMemoryIds, setForgettingMemoryIds] = useState<Set<number>>(new Set());
  const [conversationId, setConversationId] = useState<number | undefined>();
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [responseStatus, setResponseStatus] = useState<ResponseStatus>("ready");
  const [messages, setMessages] = useState<ChatMessage[]>([
    initialAssistantMessage()
  ]);
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [order, setOrder] = useState<OrderCard | null>(null);
  const [orders, setOrders] = useState<OrderSummary[]>([]);
  const [orderQuery, setOrderQuery] = useState<OrderQueryMeta | null>(null);
  const [boundary, setBoundary] = useState<BoundaryClassification | null>(null);
  const [evidence, setEvidence] = useState<EvidenceItem[]>([]);
  const [suggestedActions, setSuggestedActions] = useState<SuggestedAction[]>([]);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [handoffNotice, setHandoffNotice] = useState<HandoffNotice | null>(null);
  const [ticketReason, setTicketReason] = useState("商品不符合预期");
  const [ticketType, setTicketType] = useState<HandoffRequestType>("return");
  const [handoffSubmitting, setHandoffSubmitting] = useState(false);
  const [handoffQueryId, setHandoffQueryId] = useState("");
  const [handoffQueryLoading, setHandoffQueryLoading] = useState(false);
  const [handoffQueryError, setHandoffQueryError] = useState<string | null>(null);
  const [handoffQueryResult, setHandoffQueryResult] = useState<HandoffRequest | null>(null);
  const [error, setError] = useState<RequestError | null>(null);
  const [failedRequest, setFailedRequest] = useState<PendingRequest | null>(null);
  const [highlightedProductId, setHighlightedProductId] = useState<number | null>(null);
  const [contextPanelCollapsed, setContextPanelCollapsed] = useState(false);
  const [activeMobileTab, setActiveMobileTab] = useState<MobileTab>("chat");
  const isMobileContext = useMediaQuery("(max-width: 820px)");

  const handleProductClick = useCallback((product: ProductCard) => {
    setHighlightedProductId(product.sku_id);
    setTimeout(() => setHighlightedProductId(null), 2000);
  }, []);

  useEffect(() => {
    let cancelled = false;
    restoreSession()
      .then((session) => {
        if (cancelled) return;
        setAuthSession(session);
        setAuthError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        setAuthSession(null);
        setAuthError(err instanceof Error ? err.message : "会话恢复失败，请重新登录。");
      })
      .finally(() => {
        if (!cancelled) setAuthStatus("ready");
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!authSession) return;
    void refreshConversations();
    void refreshMemories();
  }, [authSession?.user.id]);

  async function handleLogin(loginIdentifier: string, password: string) {
    setAuthStatus("submitting");
    setAuthError(null);
    try {
      const session = await login(loginIdentifier, password);
      setAuthSession(session);
      resetWorkspace();
    } catch (err) {
      setAuthSession(null);
      setAuthError(err instanceof Error ? err.message : "登录失败，请稍后重试。");
    } finally {
      setAuthStatus("ready");
    }
  }

  async function handleLogout() {
    const logoutRequest = logout();
    setAuthSession(null);
    setAuthError(null);
    resetWorkspace();
    await logoutRequest;
  }

  function handleAuthExpired() {
    clearAuthSession();
    setAuthSession(null);
    setAuthError("登录已过期，请重新登录。");
    resetWorkspace();
  }

  function resetWorkspace() {
    requestAbortRef.current?.abort();
    requestAbortRef.current = null;
    memoryRequestVersionRef.current += 1;
    workspaceVersionRef.current += 1;
    conversationListRequestVersionRef.current += 1;
    conversationDetailRequestVersionRef.current += 1;
    setConversations([]);
    setConversationsLoading(false);
    setMemories([]);
    setMemoriesLoading(false);
    setMemoryError(null);
    setForgettingMemoryIds(new Set());
    resetActiveConversation();
  }

  function resetActiveConversation() {
    setConversationId(undefined);
    setInput("");
    setLoading(false);
    setResponseStatus("ready");
    setMessages([initialAssistantMessage()]);
    setProducts([]);
    setOrder(null);
    setOrders([]);
    setOrderQuery(null);
    setBoundary(null);
    setEvidence([]);
    setSuggestedActions([]);
    setTurns([]);
    setHandoffNotice(null);
    setHandoffSubmitting(false);
    setHandoffQueryId("");
    setHandoffQueryLoading(false);
    setHandoffQueryError(null);
    setHandoffQueryResult(null);
    setError(null);
    setFailedRequest(null);
  }

  async function refreshConversations() {
    const workspaceVersion = workspaceVersionRef.current;
    const requestVersion = ++conversationListRequestVersionRef.current;
    setConversationsLoading(true);
    try {
      const nextConversations = await listConversations();
      if (
        workspaceVersion !== workspaceVersionRef.current ||
        requestVersion !== conversationListRequestVersionRef.current
      ) return;
      setConversations(nextConversations);
    } catch (err) {
      if (
        workspaceVersion !== workspaceVersionRef.current ||
        requestVersion !== conversationListRequestVersionRef.current
      ) return;
      const requestError = toRequestError(err, { message: "load conversations" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
      }
    } finally {
      if (
        workspaceVersion === workspaceVersionRef.current &&
        requestVersion === conversationListRequestVersionRef.current
      ) {
        setConversationsLoading(false);
      }
    }
  }

  async function refreshMemories() {
    const requestVersion = ++memoryRequestVersionRef.current;
    setMemoriesLoading(true);
    setMemoryError(null);
    try {
      const nextMemories = await listMemories();
      if (requestVersion !== memoryRequestVersionRef.current) return;
      setMemories(nextMemories);
    } catch (err) {
      if (requestVersion !== memoryRequestVersionRef.current) return;
      const requestError = toRequestError(err, { message: "load memories" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setMemoryError("偏好记忆加载失败，请稍后重试。");
    } finally {
      if (requestVersion === memoryRequestVersionRef.current) {
        setMemoriesLoading(false);
      }
    }
  }

  async function handleForgetMemory(memoryId: number) {
    if (forgettingMemoryIds.has(memoryId)) return;
    const workspaceVersion = workspaceVersionRef.current;
    setForgettingMemoryIds((current) => new Set(current).add(memoryId));
    setMemoryError(null);
    try {
      await forgetMemory(memoryId);
      if (workspaceVersion !== workspaceVersionRef.current) return;
      memoryRequestVersionRef.current += 1;
      setMemoriesLoading(false);
      setMemories((current) => current.filter((memory) => memory.id !== memoryId));
    } catch (err) {
      if (workspaceVersion !== workspaceVersionRef.current) return;
      const requestError = toRequestError(err, { message: "forget memory" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setMemoryError(requestError.message);
    } finally {
      if (workspaceVersion === workspaceVersionRef.current) {
        setForgettingMemoryIds((current) => {
          const next = new Set(current);
          next.delete(memoryId);
          return next;
        });
      }
    }
  }

  function handleNewConversation() {
    if (loading) return;
    resetActiveConversation();
  }

  async function handleDeleteConversation(targetId: number) {
    const workspaceVersion = workspaceVersionRef.current;
    try {
      await deleteConversationApi(targetId);
      if (workspaceVersion !== workspaceVersionRef.current) return;
      if (targetId === conversationId) {
        resetActiveConversation();
      }
      if (workspaceVersion === workspaceVersionRef.current) {
        await refreshConversations();
      }
    } catch (err) {
      if (workspaceVersion !== workspaceVersionRef.current) return;
      const requestError = toRequestError(err, { message: "delete conversation" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
      }
    }
  }

  async function handleSelectConversation(nextConversationId: number) {
    if (loading || nextConversationId === conversationId) return;
    const workspaceVersion = workspaceVersionRef.current;
    const requestVersion = ++conversationDetailRequestVersionRef.current;
    setError(null);
    setFailedRequest(null);
    setConversationsLoading(true);
    try {
      const detail = await getConversation(nextConversationId);
      if (
        workspaceVersion !== workspaceVersionRef.current ||
        requestVersion !== conversationDetailRequestVersionRef.current
      ) return;
      applyConversationDetail(detail);
    } catch (err) {
      if (
        workspaceVersion !== workspaceVersionRef.current ||
        requestVersion !== conversationDetailRequestVersionRef.current
      ) return;
      const requestError = toRequestError(err, { message: "load conversation" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setError({
        ...requestError,
        message: "会话加载失败，请稍后重试。"
      });
      setResponseStatus("error");
    } finally {
      if (
        workspaceVersion === workspaceVersionRef.current &&
        requestVersion === conversationDetailRequestVersionRef.current
      ) {
        setConversationsLoading(false);
      }
    }
  }

  async function submitMessage(message: string, options: SubmitOptions = {}) {
    const trimmed = message.trim();
    if (!trimmed || loading) return;

    const shouldAppendUser = options.appendUser !== false;
    const userMessageId = options.messageId ?? crypto.randomUUID();
    const requestConversationId = options.conversationId ?? conversationId;
    const request: PendingRequest = {
      message: trimmed,
      conversationId: requestConversationId,
      messageId: userMessageId
    };

    setInput("");
    setError(null);
    setFailedRequest(null);
    setLoading(true);
    setResponseStatus("loading");
    setSuggestedActions([]);

    if (shouldAppendUser) {
      setMessages((current) => [
        ...current,
        {
          id: userMessageId,
          role: "user",
          content: trimmed,
          createdAt: new Date().toISOString(),
          status: "sent"
        }
      ]);
    } else {
      markMessageStatus(userMessageId, "sent");
    }

    const assistantMessageId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
        status: "pending",
        streamStage: "正在处理"
      }
    ]);

    const abortController = new AbortController();
    requestAbortRef.current = abortController;

    try {
      const response = await sendChat(
        trimmed,
        requestConversationId,
        abortController.signal
      );
      applyFinalResponse(response, assistantMessageId, trimmed);
      if (response.memory_changes?.length) {
        void refreshMemories();
      }
      await refreshConversations();
    } catch (err) {
      const requestError = toRequestError(err, request);
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      if (requestError.status === 499) {
        setResponseStatus("cancelled");
        setSuggestedActions([]);
        updateMessage(assistantMessageId, (message) => ({
          ...message,
          status: "cancelled",
          streamStage: "已取消"
        }));
        return;
      }
      setError(requestError);
      setFailedRequest(request);
      setResponseStatus("error");
      setSuggestedActions([]);
      markMessageStatus(userMessageId, "failed");
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        status: "failed",
        content: message.content || requestError.message,
        streamStage: undefined
      }));
    } finally {
      setLoading(false);
      if (requestAbortRef.current === abortController) {
        requestAbortRef.current = null;
      }
    }
  }

  function applyFinalResponse(
    response: ChatResponse,
    assistantMessageId: string,
    userMessage: string
  ) {
    const receivedAt = new Date().toISOString();
    const currentTurnOrderId = response.order?.id;
    const contextOrderId = response.order?.id ?? order?.id;
    setConversationId(response.conversation_id);
    setBoundary(response.boundary);
    setEvidence(isBlockedBoundary(response.boundary) ? [] : response.evidence);
    setProducts(isBlockedBoundary(response.boundary) ? [] : response.products);
    setOrder((current) => {
      if (isBlockedBoundary(response.boundary)) return null;
      if (response.order_query) return response.order ?? null;
      return response.order ?? current;
    });
    if (isBlockedBoundary(response.boundary)) {
      setOrders([]);
      setOrderQuery(null);
    } else if (response.order_query) {
      setOrders(response.orders);
      setOrderQuery(response.order_query);
    }
    setSuggestedActions(response.suggested_actions);
    updateMessage(assistantMessageId, (message) => ({
      ...message,
      content: response.answer,
      createdAt: receivedAt,
      status: "received",
      streamStage: undefined,
      boundary: response.boundary,
      intent: response.intent,
      evidenceCount: response.evidence.length,
      productCount: response.products.length,
      orderId: currentTurnOrderId,
      suggestedActions: response.suggested_actions,
      products: isBlockedBoundary(response.boundary) ? [] : response.products
    }));
    setTurns((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        userMessage,
        assistantAnswer: response.answer,
        intent: response.intent,
        boundary: response.boundary,
        evidenceCount: response.evidence.length,
        productCount: response.products.length,
        orderId: currentTurnOrderId,
        suggestedActions: response.suggested_actions,
        createdAt: receivedAt
      }
    ]);

    if (response.boundary.classification === "human_handoff_required") {
      setHandoffNotice({
        requested: false,
        source: "边界分类",
        reason: response.boundary.reason,
        orderId: contextOrderId,
        updatedAt: receivedAt
      });
    } else {
      setHandoffNotice(null);
    }
    setResponseStatus(statusForBoundary(response.boundary));
  }

  function updateMessage(messageId: string, updater: (message: ChatMessage) => ChatMessage) {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? updater(message) : message))
    );
  }

  function markMessageStatus(messageId: string, status: "sent" | "failed") {
    setMessages((current) =>
      current.map((message) => (message.id === messageId ? { ...message, status } : message))
    );
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitMessage(input);
  }

  function handleCancelRequest() {
    requestAbortRef.current?.abort();
  }

  function handleRetry() {
    if (!failedRequest) return;
    void submitMessage(failedRequest.message, {
      appendUser: false,
      conversationId: failedRequest.conversationId,
      messageId: failedRequest.messageId
    });
  }

  function handleSuggestedAction(action: SuggestedAction) {
    const message = typeof action.payload.message === "string" ? action.payload.message : null;
    if (message) {
      void submitMessage(message);
      return;
    }

    const orderId = numberFromPayload(action.payload.orderId) ?? order?.id;
    if (action.payload.handoff === true || orderId || action.label.includes("人工")) {
      setHandoffNotice({
        requested: false,
        source: action.label,
        reason: boundary?.reason ?? "需要人工确认",
        orderId,
        updatedAt: new Date().toISOString()
      });
      setResponseStatus("handoff");
      setSuggestedActions([]);
    }
  }

  async function handleRequestHandoff() {
    const reason = ticketReason.trim();
    if (!conversationId) {
      setError({
        message: "请先在当前会话中说明诉求，再记录人工确认请求。",
        retryable: false,
        request: { message: "create handoff request" }
      });
      setResponseStatus("error");
      return;
    }
    if (!reason) return;
    if (handoffNotice?.requestId) return;

    setHandoffSubmitting(true);
    setHandoffQueryError(null);
    setError(null);
    try {
      const response = await createHandoffRequest({
        session_id: conversationId,
        order_id: order?.id,
        request_type: ticketType,
        reason
      });
      const now = new Date().toISOString();
      setHandoffNotice({
        requested: true,
        source: "人工确认诉求",
        reason,
        orderId: order?.id,
        requestId: response.request_id,
        status: response.status,
        message: response.message,
        updatedAt: now
      });
      setHandoffQueryId(String(response.request_id));
      setHandoffQueryResult(null);
      setResponseStatus("handoff");
      setSuggestedActions([]);
    } catch (err) {
      const requestError = toRequestError(err, { message: "create handoff request" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setError(requestError);
      setResponseStatus("error");
    } finally {
      setHandoffSubmitting(false);
    }
  }

  function handleAcknowledgeHandoff() {
    void handleRequestHandoff();
  }

  async function handleQueryHandoffRequest() {
    const requestId = Number(handoffQueryId.trim() || handoffNotice?.requestId);
    if (!Number.isInteger(requestId) || requestId <= 0) {
      setHandoffQueryError("请输入有效的请求编号。");
      return;
    }

    setHandoffQueryLoading(true);
    setHandoffQueryError(null);
    try {
      const result = await getHandoffRequest(requestId);
      setHandoffQueryResult(result);
      setHandoffNotice((current) => ({
        requested: true,
        source: current?.source ?? "人工确认诉求",
        reason: result.reason,
        orderId: result.order_id ?? undefined,
        requestId: result.id,
        status: result.status,
        message: current?.message,
        updatedAt: result.updated_at
      }));
      setResponseStatus("handoff");
    } catch (err) {
      const requestError = toRequestError(err, { message: "query handoff request" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setHandoffQueryError(requestError.message);
    } finally {
      setHandoffQueryLoading(false);
    }
  }

  function applyConversationDetail(detail: ConversationDetail) {
    const restoredMessages = detail.messages.map(messageFromHistory);
    const lastAssistant = [...detail.messages]
      .reverse()
      .find((message) => message.role === "assistant" && message.metadata);
    const metadata = lastAssistant?.metadata ?? {};
    const restoredBoundary = boundaryFromMetadata(metadata);
    const restoredEvidence = listFromMetadata<EvidenceItem>(metadata.evidence);
    const restoredProducts = listFromMetadata<ProductCard>(metadata.products);
    const restoredOrder = orderFromMetadata(metadata.order);
    const restoredOrders = listFromMetadata<OrderSummary>(metadata.orders);
    const restoredOrderQuery = orderQueryFromMetadata(metadata.order_query);

    setConversationId(detail.id);
    setInput("");
    setMessages(restoredMessages.length ? restoredMessages : [initialAssistantMessage()]);
    setBoundary(restoredBoundary);
    setEvidence(restoredBoundary && isBlockedBoundary(restoredBoundary) ? [] : restoredEvidence);
    setProducts(restoredBoundary && isBlockedBoundary(restoredBoundary) ? [] : restoredProducts);
    setOrder(restoredBoundary && isBlockedBoundary(restoredBoundary) ? null : restoredOrder);
    setOrders(restoredBoundary && isBlockedBoundary(restoredBoundary) ? [] : restoredOrders);
    setOrderQuery(
      restoredBoundary && isBlockedBoundary(restoredBoundary) ? null : restoredOrderQuery
    );
    setSuggestedActions([]);
    setTurns(turnsFromHistory(detail));
    setHandoffNotice(null);
    setResponseStatus(restoredBoundary ? statusForBoundary(restoredBoundary) : "ready");
  }

  if (authStatus === "restoring" || !authSession) {
    return (
      <LoginPage
        loading={authStatus === "restoring" || authStatus === "submitting"}
        error={authError}
        onLogin={handleLogin}
      />
    );
  }

  const operatorProfile: OperatorProfile = {
    name: authSession.user.display_name,
    role: "PC 外设专员",
    userId: authSession.user.id,
    loginIdentifier: authSession.user.login_identifier,
    authState: "authenticated",
    statusLabel: "已登录"
  };

  return (
    <div className={`shell ${contextPanelCollapsed ? "shell-collapsed" : ""}`}>
      <Sidebar
        operator={operatorProfile}
        conversations={conversations}
        activeConversationId={conversationId}
        conversationsLoading={conversationsLoading}
        disabled={loading}
        onLogout={() => void handleLogout()}
        onNewConversation={handleNewConversation}
        onSelectConversation={(id) => void handleSelectConversation(id)}
        onDeleteConversation={handleDeleteConversation}
      />

      <div className="chat-area">
        <ChatPanel
          conversationId={conversationId}
          messages={messages}
          input={input}
          loading={loading}
          responseStatus={responseStatus}
          boundary={boundary}
          suggestedActions={suggestedActions}
          error={error}
          onInputChange={setInput}
          onSubmit={handleSubmit}
          onCancel={handleCancelRequest}
          onRetry={handleRetry}
          onSuggestedAction={handleSuggestedAction}
          onProductClick={handleProductClick}
        />
        <MobileTabBar
          activeTab={activeMobileTab}
          onTabChange={setActiveMobileTab}
          productCount={products.length}
          evidenceCount={evidence.length}
        />
      </div>

      <ContextPanel
        boundary={boundary}
        evidence={evidence}
        products={products}
        order={order}
        orders={orders}
        orderQuery={orderQuery}
        turns={turns}
        memories={memories}
        memoriesLoading={memoriesLoading}
        memoryError={memoryError}
        forgettingMemoryIds={forgettingMemoryIds}
        handoffNotice={handoffNotice}
        ticketType={ticketType}
        ticketReason={ticketReason}
        loading={loading || handoffSubmitting}
        handoffQueryId={handoffQueryId}
        handoffQueryLoading={handoffQueryLoading}
        handoffQueryError={handoffQueryError}
        handoffQueryResult={handoffQueryResult}
        skuCount={products.length}
        orderCount={orderQuery?.total_match_count ?? (order ? 1 : orders.length)}
        evidenceCount={evidence.length}
        highlightedProductId={highlightedProductId}
        mobileTab={isMobileContext ? activeMobileTab : undefined}
        onTicketTypeChange={(value) => setTicketType(value as HandoffRequestType)}
        onTicketReasonChange={setTicketReason}
        onRequestHandoff={handleRequestHandoff}
        onAcknowledgeHandoff={handleAcknowledgeHandoff}
        onHandoffQueryIdChange={setHandoffQueryId}
        onQueryHandoffRequest={handleQueryHandoffRequest}
        onForgetMemory={(memoryId) => void handleForgetMemory(memoryId)}
      />

      <button
        type="button"
        className="panel-toggle"
        onClick={() => setContextPanelCollapsed((c) => !c)}
        title={contextPanelCollapsed ? "展开详情" : "收起详情"}
      >
        {contextPanelCollapsed ? <PanelRightOpen size={18} /> : <PanelRightClose size={18} />}
      </button>
    </div>
  );
}

function statusForBoundary(boundary: BoundaryClassification): ResponseStatus {
  if (boundary.classification === "human_handoff_required") return "handoff";
  if (isBlockedBoundary(boundary)) return "blocked";
  return "success";
}

function isBlockedBoundary(boundary: BoundaryClassification): boolean {
  return (
    boundary.classification === "out_of_scope" ||
    boundary.classification === "security_refusal"
  );
}

function initialAssistantMessage(): ChatMessage {
  return {
    id: "hello",
    role: "assistant",
    content: "今天想看哪类外设？",
    createdAt: new Date().toISOString(),
    status: "received"
  };
}

function messageFromHistory(message: ConversationDetail["messages"][number]): ChatMessage {
  const metadata = message.metadata ?? {};
  const boundary = boundaryFromMetadata(metadata);
  const evidence = listFromMetadata<EvidenceItem>(metadata.evidence);
  const products = listFromMetadata<ProductCard>(metadata.products);
  const order = orderFromMetadata(metadata.order);
  return {
    id: String(message.id),
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
    status: "received",
    boundary: boundary ?? undefined,
    intent: typeof metadata.intent === "string" ? metadata.intent : undefined,
    evidenceCount: evidence.length || undefined,
    productCount: products.length || undefined,
    orderId: order?.id,
    products: products.length > 0 ? products : undefined
  };
}

function turnsFromHistory(detail: ConversationDetail): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (let index = 0; index < detail.messages.length; index += 1) {
    const user = detail.messages[index];
    const assistant = detail.messages[index + 1];
    if (user?.role !== "user" || assistant?.role !== "assistant") continue;
    const metadata = assistant.metadata ?? {};
    const boundary = boundaryFromMetadata(metadata);
    if (!boundary) continue;
    const evidence = listFromMetadata<EvidenceItem>(metadata.evidence);
    const products = listFromMetadata<ProductCard>(metadata.products);
    const order = orderFromMetadata(metadata.order);
    turns.push({
      id: `${user.id}-${assistant.id}`,
      userMessage: user.content,
      assistantAnswer: assistant.content,
      intent: typeof metadata.intent === "string" ? metadata.intent : "unknown",
      boundary,
      evidenceCount: evidence.length,
      productCount: products.length,
      orderId: order?.id,
      suggestedActions: [],
      createdAt: assistant.created_at
    });
  }
  return turns;
}

function boundaryFromMetadata(metadata: Record<string, unknown>): BoundaryClassification | null {
  const boundary = metadata.boundary;
  if (!isRecord(boundary)) return null;
  const classification = boundary.classification;
  if (
    classification !== "in_scope_auto" &&
    classification !== "human_handoff_required" &&
    classification !== "out_of_scope" &&
    classification !== "security_refusal"
  ) {
    return null;
  }
  return {
    classification,
    reason: typeof boundary.reason === "string" ? boundary.reason : "",
    display_message:
      typeof boundary.display_message === "string" ? boundary.display_message : ""
  };
}

function orderQueryFromMetadata(value: unknown): OrderQueryMeta | null {
  if (!isRecord(value)) return null;
  const mode = value.query_mode;
  const validModes = [
    "explicit",
    "latest",
    "recent",
    "all",
    "count",
    "page",
    "analysis"
  ];
  if (typeof mode !== "string" || !validModes.includes(mode)) return null;
  return value as OrderQueryMeta;
}

function listFromMetadata<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function orderFromMetadata(value: unknown): OrderCard | null {
  return isRecord(value) && typeof value.id === "number" ? (value as OrderCard) : null;
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mediaQuery = window.matchMedia(query);
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches);

    setMatches(mediaQuery.matches);
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [query]);

  return matches;
}

function toRequestError(error: unknown, request: PendingRequest): RequestError {
  if (error instanceof ApiError) {
    return {
      message: error.message,
      retryable: error.retryable,
      status: error.status,
      request
    };
  }
  if (error instanceof Error) {
    return {
      message: error.message,
      retryable: true,
      request
    };
  }
  return {
    message: "请求失败，请稍后重试。",
    retryable: true,
    request
  };
}

function numberFromPayload(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
