import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
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
import { DetailDrawer } from "./components/DetailDrawer";
import { ProductResultsDrawer } from "./components/ProductResultsDrawer";
import { PreferencesPanel, HandoffPanel } from "./components/ServicePanel";
import { LoginPage } from "./components/LoginPage";
import { Sidebar } from "./components/Sidebar";
import type {
  AuthSession,
  BoundaryClassification,
  ChatResponse,
  ChatMessage,
  ConversationDetail,
  ConversationSummary,
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
  const [messages, setMessages] = useState<ChatMessage[]>([
    initialAssistantMessage()
  ]);
  const [order, setOrder] = useState<OrderCard | null>(null);
  const [boundary, setBoundary] = useState<BoundaryClassification | null>(null);
  const [suggestedActions, setSuggestedActions] = useState<SuggestedAction[]>([]);
  const [handoffNotice, setHandoffNotice] = useState<HandoffNotice | null>(null);
  const [ticketReason, setTicketReason] = useState("");
  const [ticketType, setTicketType] = useState<HandoffRequestType>("return");
  const [handoffSubmitting, setHandoffSubmitting] = useState(false);
  const [handoffQueryId, setHandoffQueryId] = useState("");
  const [handoffQueryLoading, setHandoffQueryLoading] = useState(false);
  const [handoffQueryError, setHandoffQueryError] = useState<string | null>(null);
  const [handoffQueryResult, setHandoffQueryResult] = useState<HandoffRequest | null>(null);
  const [error, setError] = useState<RequestError | null>(null);
  const [failedRequest, setFailedRequest] = useState<PendingRequest | null>(null);
  const [productResults, setProductResults] = useState<ProductCard[] | null>(null);
  const [selectedProduct, setSelectedProduct] = useState<ProductCard | null>(null);
  const [utilityPanel, setUtilityPanel] = useState<"preferences" | "handoff" | null>(null);
  const isMobileContext = useMediaQuery("(max-width: 820px)");
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    try { return !window.matchMedia("(max-width: 820px)").matches && localStorage.getItem("pc-sidebar-open") !== "false"; }
    catch { return true; }
  });
  function toggleSidebar() {
    setSidebarOpen(current => {
      try { localStorage.setItem("pc-sidebar-open", String(!current)); } catch { /* storage may be disabled */ }
      return !current;
    });
  }
  useEffect(() => { if (isMobileContext) setSidebarOpen(false); }, [isMobileContext]);
  const handleProductClick = useCallback((product: ProductCard) => {
    setUtilityPanel(null);
    setProductResults(null);
    setSelectedProduct(product);
  }, []);
  function prepareQuestion(text: string) {
    setInput(text);
    setSelectedProduct(null); setProductResults(null);
  }
  function openHandoff() {
    setSelectedProduct(null); setProductResults(null);
    if (!handoffNotice) setHandoffNotice({ requested: false, source: "人工诉求", reason: "需要人工确认", orderId: order?.id, updatedAt: new Date().toISOString() });
    setUtilityPanel("handoff");
  }

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
    setSelectedProduct(null); setProductResults(null);
    setUtilityPanel(null);
    conversationDetailRequestVersionRef.current += 1;
    setConversationId(undefined);
    setInput("");
    setLoading(false);
    setMessages([initialAssistantMessage()]);
    setOrder(null);
    setBoundary(null);
    setSuggestedActions([]);
    setHandoffNotice(null);
    setHandoffSubmitting(false);
    setHandoffQueryId("");
    setHandoffQueryLoading(false);
    setHandoffQueryError(null);
    setHandoffQueryResult(null);
    setError(null);
    setFailedRequest(null);
    setTicketReason("");
    setTicketType("return");
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
    if (isMobileContext) setSidebarOpen(false);
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
      setSelectedProduct(null); setProductResults(null);
      setUtilityPanel(null);
      if (isMobileContext) setSidebarOpen(false);
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
    if (loading || requestAbortRef.current) return;

    const shouldAppendUser = options.appendUser !== false;
    const userMessageId = options.messageId ?? crypto.randomUUID();
    const requestConversationId = options.conversationId ?? conversationId;
    const request: PendingRequest = {
      message: message,
      conversationId: requestConversationId,
      messageId: userMessageId
    };

    setInput("");
    setError(null);
    setFailedRequest(null);
    setLoading(true);
    setSuggestedActions([]);

    if (shouldAppendUser) {
      setMessages((current) => [
        ...current,
        {
          id: userMessageId,
          role: "user",
          content: message,
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
        requestStage: "正在处理"
      }
    ]);

    const abortController = new AbortController();
    requestAbortRef.current = abortController;

    try {
      const response = await sendChat(
        message,
        requestConversationId,
        abortController.signal
      );
      applyFinalResponse(response, assistantMessageId);
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
        setSuggestedActions([]);
        updateMessage(assistantMessageId, (message) => ({
          ...message,
          status: "cancelled",
          requestStage: "已取消"
        }));
        return;
      }
      setError(requestError);
      setFailedRequest(request);
      setSuggestedActions([]);
      markMessageStatus(userMessageId, "failed");
      updateMessage(assistantMessageId, (message) => ({
        ...message,
        status: "failed",
        content: message.content || requestError.message,
        requestStage: undefined
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
    assistantMessageId: string
  ) {
    const receivedAt = new Date().toISOString();
    const currentTurnOrderId = response.order?.id;
    const contextOrderId = response.order?.id ?? order?.id;
    const hasOrderResults = response.order_query != null ||
      response.execution_details?.tool_calls.some(
        (call) => call.tool_name === "order_lookup" &&
          ["usable", "empty", "not_found"].includes(call.outcome)
      );
    setConversationId(response.conversation_id);
    setBoundary(response.boundary);
    setOrder((current) => {
      if (isBlockedBoundary(response.boundary)) return null;
      if (hasOrderResults) return response.order ?? null;
      return response.order ?? current;
    });
    setSuggestedActions(response.suggested_actions);
    updateMessage(assistantMessageId, (message) => ({
      ...message,
      content: response.answer,
      createdAt: receivedAt,
      status: "received",
      requestStage: undefined,
      boundary: response.boundary,
      intent: response.intent,
      evidenceCount: response.evidence.length,
      productCount: response.products.length,
      orderId: currentTurnOrderId,
      suggestedActions: response.suggested_actions,
      products: isBlockedBoundary(response.boundary) ? [] : response.products,
      order: isBlockedBoundary(response.boundary) ? null : response.order,
      orders: isBlockedBoundary(response.boundary) ? [] : response.orders,
      orderQuery: isBlockedBoundary(response.boundary) ? null : response.order_query,
      evidence: isBlockedBoundary(response.boundary) ? [] : response.evidence
    }));

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
      return;
    }
    if (!reason) return;
    if (handoffNotice?.requestId) return;
    const orderId = handoffNotice?.orderId ?? order?.id;

    setHandoffSubmitting(true);
    setHandoffQueryError(null);
    setError(null);
    try {
      const response = await createHandoffRequest({
        session_id: conversationId,
        order_id: orderId,
        request_type: ticketType,
        reason
      });
      const now = new Date().toISOString();
      setHandoffNotice({
        requested: true,
        source: "人工确认诉求",
        reason,
        orderId,
        requestId: response.request_id,
        status: response.status,
        message: response.message,
        updatedAt: now
      });
      setHandoffQueryId(String(response.request_id));
      setHandoffQueryResult(null);
      setSuggestedActions([]);
    } catch (err) {
      const requestError = toRequestError(err, { message: "create handoff request" });
      if (requestError.status === 401 || requestError.status === 403) {
        handleAuthExpired();
        return;
      }
      setError(requestError);
    } finally {
      setHandoffSubmitting(false);
    }
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
    const restoredOrder = orderFromMetadata(metadata.order);

    setConversationId(detail.id);
    setInput("");
    setMessages(restoredMessages.length ? restoredMessages : [initialAssistantMessage()]);
    setBoundary(restoredBoundary);
    setOrder(restoredBoundary && isBlockedBoundary(restoredBoundary) ? null : restoredOrder);
    setSuggestedActions([]);
    setHandoffNotice(null);
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
    <div className={`shell ${sidebarOpen ? "sidebar-expanded" : "sidebar-collapsed"}`}>
      {isMobileContext && sidebarOpen && <button className="sidebar-backdrop" aria-label="关闭历史咨询" onClick={() => setSidebarOpen(false)} />}
      {sidebarOpen && <Sidebar
        operator={operatorProfile}
        conversations={conversations}
        activeConversationId={conversationId}
        conversationsLoading={conversationsLoading}
        disabled={loading || handoffSubmitting}
        onClose={toggleSidebar}
        onPreferences={() => { setSelectedProduct(null); setProductResults(null); setUtilityPanel("preferences"); }}
        onLogout={() => void handleLogout()}
        onNewConversation={handleNewConversation}
        onSelectConversation={(id) => void handleSelectConversation(id)}
        onDeleteConversation={handleDeleteConversation}
      />}
      <ChatPanel
        key={conversationId ?? "new"}
        title={conversations.find(item => item.id === conversationId)?.title}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={toggleSidebar}
        messages={messages}
        input={input}
        loading={loading || handoffSubmitting}
        suggestedActions={suggestedActions}
        error={error}
        onInputChange={setInput}
        onSubmit={handleSubmit}
        onCancel={handleCancelRequest}
        onRetry={handleRetry}
        onSuggestedAction={action => { handleSuggestedAction(action); if (typeof action.payload.message !== "string") { setSelectedProduct(null); setProductResults(null); setUtilityPanel("handoff"); } }}
        onProductClick={handleProductClick}
        onViewProducts={products => { setUtilityPanel(null); setSelectedProduct(null); setProductResults(products); }}
        onAsk={prepareQuestion}
        onHandoff={openHandoff}
      />
      {(selectedProduct || productResults) && <ProductResultsDrawer
        products={productResults ?? [selectedProduct!]} initialProduct={selectedProduct}
        onClose={() => { setSelectedProduct(null); setProductResults(null); }} onAsk={prepareQuestion} />}
      {utilityPanel && <DetailDrawer title={utilityPanel === "preferences" ? "偏好管理" : "人工诉求"} onClose={() => { if (!handoffSubmitting) setUtilityPanel(null); }}>
        {utilityPanel === "preferences" ? <PreferencesPanel memories={memories} loading={memoriesLoading}
          error={memoryError} forgetting={forgettingMemoryIds} onForget={id => void handleForgetMemory(id)} />
          : <><HandoffPanel notice={handoffNotice} type={ticketType} reason={ticketReason}
            loading={loading || handoffSubmitting} queryId={handoffQueryId} queryLoading={handoffQueryLoading}
            queryError={handoffQueryError} queryResult={handoffQueryResult}
            onType={setTicketType} onReason={setTicketReason} onSubmit={() => void handleRequestHandoff()}
            onQueryId={setHandoffQueryId} onQuery={() => void handleQueryHandoffRequest()} />
            {error && <p role="alert" className="input-limit">{error.message}</p>}</>}
      </DetailDrawer>}
    </div>
  );
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
    products: boundary && isBlockedBoundary(boundary) ? [] : products,
    order: boundary && isBlockedBoundary(boundary) ? null : order,
    orders: boundary && isBlockedBoundary(boundary) ? [] : listFromMetadata<OrderSummary>(metadata.orders),
    orderQuery: boundary && isBlockedBoundary(boundary) ? null : orderQueryFromMetadata(metadata.order_query),
    evidence: boundary && isBlockedBoundary(boundary) ? [] : evidence
  };
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
