import type {
  AuthSession,
  AuthTokenResponse,
  AuthUser,
  ChatResponse,
  ChatStreamEvent,
  ConversationDetail,
  ConversationSummary,
  HandoffRequest,
  HandoffRequestAccepted,
  HandoffRequestType,
  MemoryItem
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const ACCESS_TOKEN_KEY = "pc-agent.accessToken";
const REFRESH_TOKEN_KEY = "pc-agent.refreshToken";
const EXPIRES_IN_KEY = "pc-agent.expiresIn";
let authSessionGeneration = 0;
let refreshSessionFlight: RefreshSessionFlight | null = null;

export type AuthSessionSnapshot = {
  generation: number;
  accessToken: string | null;
  refreshToken: string | null;
};

type RefreshSessionFlight = {
  snapshot: RefreshAuthSessionSnapshot;
  promise: Promise<AuthSession>;
};

type RefreshAuthSessionSnapshot = AuthSessionSnapshot & {
  refreshToken: string;
};

type ApiErrorOptions = {
  status?: number;
  detail?: unknown;
  retryable?: boolean;
};

export class ApiError extends Error {
  status?: number;
  detail?: unknown;
  retryable: boolean;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.detail = options.detail;
    this.retryable = options.retryable ?? false;
  }
}

export async function login(loginIdentifier: string, password: string): Promise<AuthSession> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        login_identifier: loginIdentifier,
        password
      })
    });
  } catch {
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }

  authSessionGeneration += 1;
  return saveAuthSession((await response.json()) as AuthTokenResponse);
}

export async function restoreSession(): Promise<AuthSession | null> {
  const stored = readStoredSession();
  if (!stored) return null;

  const response = await authorizedFetch("/api/auth/me", { method: "GET" });
  if (response.ok) {
    const user = (await response.json()) as AuthUser;
    return { ...readStoredSessionOrThrow(), user };
  }

  if (response.status === 401 || response.status === 403) {
    clearAuthSession();
    return null;
  }

  const detail = await parseErrorDetail(response);
  throw new ApiError(formatApiError(response.status, detail), {
    status: response.status,
    detail,
    retryable: response.status >= 500
  });
}

export function refreshSession(): Promise<AuthSession> {
  return refreshSessionForSnapshot(readAuthSessionSnapshot());
}

function refreshSessionForSnapshot(snapshot: AuthSessionSnapshot): Promise<AuthSession> {
  if (!snapshot.refreshToken) {
    clearAuthSession();
    return Promise.reject(new ApiError("登录已过期，请重新登录。", { status: 401 }));
  }
  const refreshSnapshot: RefreshAuthSessionSnapshot = {
    ...snapshot,
    refreshToken: snapshot.refreshToken
  };
  if (
    refreshSessionFlight &&
    refreshSessionSnapshotsMatch(
      refreshSessionFlight.snapshot,
      refreshSnapshot
    )
  ) {
    return refreshSessionFlight.promise;
  }

  const refresh = performRefreshSession(refreshSnapshot);
  refreshSessionFlight = { snapshot: refreshSnapshot, promise: refresh };
  refresh.then(
    () => clearRefreshPromise(refresh),
    () => clearRefreshPromise(refresh)
  );
  return refresh;
}

async function performRefreshSession(
  snapshot: RefreshAuthSessionSnapshot
): Promise<AuthSession> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: snapshot.refreshToken })
    });
  } catch {
    throw new ApiError("无法连接后端服务，请稍后重试。", { retryable: true });
  }

  if (!response.ok) {
    if (!isCurrentAuthSession(snapshot.generation, snapshot.refreshToken)) {
      throw new ApiError("登录状态已变更。", { status: 401 });
    }
    clearAuthSession();
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }

  if (!isCurrentAuthSession(snapshot.generation, snapshot.refreshToken)) {
    throw new ApiError("登录状态已变更。", { status: 401 });
  }
  return saveAuthSession((await response.json()) as AuthTokenResponse);
}

function clearRefreshPromise(refresh: Promise<AuthSession>): void {
  if (refreshSessionFlight?.promise === refresh) {
    refreshSessionFlight = null;
  }
}

export function authSessionSnapshotsMatch(
  left: AuthSessionSnapshot,
  right: AuthSessionSnapshot
): boolean {
  return (
    left.generation === right.generation &&
    left.accessToken === right.accessToken &&
    left.refreshToken === right.refreshToken
  );
}

export function refreshSessionSnapshotsMatch(
  left: AuthSessionSnapshot,
  right: AuthSessionSnapshot
): boolean {
  return left.generation === right.generation && left.refreshToken === right.refreshToken;
}

export async function logout(): Promise<void> {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  clearAuthSession();
  if (!refreshToken) return;

  try {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken })
    });
  } catch {
    // Local logout should still complete when the backend is unreachable.
  }
}

export async function sendChat(
  message: string,
  conversationId?: number,
  signal?: AbortSignal
): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await authorizedFetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        conversation_id: conversationId
      }),
      signal
    });
  } catch (error) {
    if (isAbortError(error) || signal?.aborted) {
      throw new ApiError("已取消本次回答。", {
        status: 499,
        retryable: false
      });
    }
    throw error;
  }

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status === 408 || response.status === 429 || response.status >= 500
    });
  }
  return response.json();
}

export async function createHandoffRequest(payload: {
  session_id: number;
  order_id?: number;
  request_type: HandoffRequestType;
  reason: string;
}): Promise<HandoffRequestAccepted> {
  const response = await authorizedFetch("/api/after-sales", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
  return response.json();
}

export async function getHandoffRequest(requestId: number): Promise<HandoffRequest> {
  const response = await authorizedFetch(`/api/after-sales/handoff-requests/${requestId}`, {
    method: "GET"
  });

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
  return response.json();
}

type SendChatStreamOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
  onEvent: (event: ChatStreamEvent) => void;
};

export async function sendChatStream(
  message: string,
  conversationId: number | undefined,
  options: SendChatStreamOptions
): Promise<ChatResponse> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 45000;
  let timeoutTriggered = false;
  let finalResponse: ChatResponse | null = null;
  let errorEvent: Extract<ChatStreamEvent, { type: "error" }> | null = null;
  let timeoutId = window.setTimeout(() => {
    timeoutTriggered = true;
    controller.abort();
  }, timeoutMs);

  const resetTimeout = () => {
    window.clearTimeout(timeoutId);
    timeoutId = window.setTimeout(() => {
      timeoutTriggered = true;
      controller.abort();
    }, timeoutMs);
  };

  const abortFromCaller = () => controller.abort();
  if (options.signal?.aborted) {
    abortFromCaller();
  } else {
    options.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  try {
    const response = await authorizedFetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        conversation_id: conversationId
      }),
      signal: controller.signal
    });

    if (!response.ok) {
      const detail = await parseErrorDetail(response);
      throw new ApiError(formatApiError(response.status, detail), {
        status: response.status,
        detail,
        retryable: response.status === 408 || response.status === 429 || response.status >= 500
      });
    }

    if (!response.body) {
      throw new ApiError("浏览器没有收到流式响应体，请稍后重试。", { retryable: true });
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      resetTimeout();
      buffer += decoder.decode(value, { stream: true });
      const events = consumeSseEvents(buffer);
      buffer = events.remaining;

      for (const event of events.items) {
        options.onEvent(event);
        if (event.type === "done") {
          finalResponse = event.response;
        } else if (event.type === "error") {
          errorEvent = event;
        }
      }
    }

    buffer += decoder.decode();
    const events = consumeSseEvents(buffer, true);
    for (const event of events.items) {
      options.onEvent(event);
      if (event.type === "done") {
        finalResponse = event.response;
      } else if (event.type === "error") {
        errorEvent = event;
      }
    }

    if (errorEvent) {
      throw new ApiError(errorEvent.message, {
        retryable: errorEvent.retryable ?? true
      });
    }

    if (!finalResponse) {
      throw new ApiError("连接中断，AI 回答未完成，可以重试。", {
        retryable: true
      });
    }

    return finalResponse;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (isAbortError(error) || controller.signal.aborted) {
      if (timeoutTriggered) {
        throw new ApiError("等待 AI 回复超时，请重试或稍后再试。", {
          status: 408,
          retryable: true
        });
      }
      throw new ApiError("已取消本次回答。", {
        status: 499,
        retryable: false
      });
    }
    throw new ApiError("流式连接异常中断，请稍后重试。", {
      retryable: true
    });
  } finally {
    window.clearTimeout(timeoutId);
    options.signal?.removeEventListener("abort", abortFromCaller);
  }
}

export async function listConversations(): Promise<ConversationSummary[]> {
  const response = await authorizedFetch("/api/conversations", { method: "GET" });
  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
  return response.json();
}

export async function listMemories(): Promise<MemoryItem[]> {
  const response = await authorizedFetch("/api/memories", { method: "GET" });
  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
  return response.json();
}

export async function forgetMemory(memoryId: number): Promise<void> {
  const response = await authorizedFetch(`/api/memories/${memoryId}`, {
    method: "DELETE"
  });
  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
}

export async function deleteConversation(conversationId: number): Promise<void> {
  const response = await authorizedFetch(`/api/conversations/${conversationId}`, {
    method: "DELETE"
  });
  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
}

export async function getConversation(conversationId: number): Promise<ConversationDetail> {
  const response = await authorizedFetch(`/api/conversations/${conversationId}`, {
    method: "GET"
  });
  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(formatApiError(response.status, detail), {
      status: response.status,
      detail,
      retryable: response.status >= 500
    });
  }
  return response.json();
}

function saveAuthSession(payload: AuthTokenResponse): AuthSession {
  localStorage.setItem(ACCESS_TOKEN_KEY, payload.access_token);
  localStorage.setItem(REFRESH_TOKEN_KEY, payload.refresh_token);
  localStorage.setItem(EXPIRES_IN_KEY, String(payload.expires_in));
  return {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    expiresIn: payload.expires_in,
    user: payload.user
  };
}

export function clearAuthSession() {
  authSessionGeneration += 1;
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
  localStorage.removeItem(EXPIRES_IN_KEY);
}

function isCurrentAuthSession(generation: number, refreshToken: string): boolean {
  return (
    generation === authSessionGeneration &&
    localStorage.getItem(REFRESH_TOKEN_KEY) === refreshToken
  );
}

function readStoredSession(): AuthSession | null {
  const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!accessToken || !refreshToken) return null;
  return {
    accessToken,
    refreshToken,
    expiresIn: Number(localStorage.getItem(EXPIRES_IN_KEY) ?? 0),
    user: {
      id: 0,
      login_identifier: "",
      display_name: "",
      status: "unknown",
      last_login_at: null
    }
  };
}

function readStoredSessionOrThrow(): AuthSession {
  const stored = readStoredSession();
  if (!stored) {
    throw new ApiError("登录已过期，请重新登录。", { status: 401 });
  }
  return stored;
}

async function authorizedFetch(
  path: string,
  init: RequestInit = {},
  retryAuth = true
): Promise<Response> {
  const requestSnapshot = readAuthSessionSnapshot();
  let response: Response;
  try {
    response = await fetch(
      `${API_BASE}${path}`,
      withAuthHeader(init, requestSnapshot.accessToken)
    );
  } catch (error) {
    if (isAbortError(error) || init.signal?.aborted) {
      throw error;
    }
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }

  if (response.status !== 401 || !retryAuth) {
    return response;
  }

  const currentSnapshot = readAuthSessionSnapshot();
  if (
    currentSnapshot.accessToken &&
    !authSessionSnapshotsMatch(requestSnapshot, currentSnapshot)
  ) {
    return authorizedFetch(path, init, false);
  }

  try {
    await refreshSessionForSnapshot(requestSnapshot);
  } catch {
    const latestSnapshot = readAuthSessionSnapshot();
    if (
      latestSnapshot.accessToken &&
      !authSessionSnapshotsMatch(requestSnapshot, latestSnapshot)
    ) {
      return authorizedFetch(path, init, false);
    }
    return response;
  }

  try {
    return await fetch(`${API_BASE}${path}`, withAuthHeader(init));
  } catch (error) {
    if (isAbortError(error) || init.signal?.aborted) {
      throw error;
    }
    throw new ApiError("无法连接后端服务，请确认 API 或 Vite 代理已启动。", {
      retryable: true
    });
  }
}

function withAuthHeader(init: RequestInit, accessToken?: string | null): RequestInit {
  const headers = new Headers(init.headers);
  const token =
    accessToken === undefined ? localStorage.getItem(ACCESS_TOKEN_KEY) : accessToken;
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return { ...init, headers };
}

function readAuthSessionSnapshot(): AuthSessionSnapshot {
  return {
    generation: authSessionGeneration,
    accessToken: localStorage.getItem(ACCESS_TOKEN_KEY),
    refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY)
  };
}

function consumeSseEvents(
  buffer: string,
  flush = false
): { items: ChatStreamEvent[]; remaining: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const remaining = flush ? "" : (blocks.pop() ?? "");
  const completeBlocks = flush ? blocks.filter(Boolean) : blocks;
  return {
    items: completeBlocks.flatMap(parseSseBlock),
    remaining
  };
}

function parseSseBlock(block: string): ChatStreamEvent[] {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, ""))
    .join("\n")
    .trim();

  if (!data) return [];

  try {
    const payload = JSON.parse(data) as ChatStreamEvent;
    return payload && typeof payload.type === "string" ? [payload] : [];
  } catch {
    return [];
  }
}

async function parseErrorDetail(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const payload: unknown = await response.json();
      return isRecord(payload) && "detail" in payload ? payload.detail : payload;
    } catch {
      return null;
    }
  }

  try {
    const text = await response.text();
    return text || null;
  } catch {
    return null;
  }
}

function formatApiError(status: number, detail: unknown): string {
  if (isRecord(detail) && typeof detail.display_message === "string") {
    return detail.display_message;
  }
  if (isRecord(detail) && typeof detail.message === "string") {
    return detail.message;
  }
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    return "请求参数未通过校验，请检查输入后重试。";
  }
  if (status >= 500) {
    return `后端暂时不可用（HTTP ${status}），稍后可以重试。`;
  }
  return `请求失败（HTTP ${status}）。`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}
