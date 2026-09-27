export type ProductCard = {
  spu_id: number;
  sku_id: number;
  title: string;
  spu_title?: string | null;
  entity_scope?: "sku" | "spu";
  brand: string;
  category: string;
  price: string;
  stock: number;
  sku_sales_count: number;
  sales_count: number;
  specs: Record<string, string>;
  image_url?: string | null;
  ranking_scope?: "sku" | "spu" | null;
  ranking_metric?: "price" | "stock" | "sales" | null;
  ranking_value?: string | null;
  series_min_price?: string | null;
  series_max_price?: string | null;
  series_total_stock?: number | null;
  series_sku_count?: number | null;
  series_common_specs?: Record<string, string>;
  series_option_specs?: Record<string, string[]>;
  series_variants?: Array<{
    sku_id: number;
    title: string;
    price: string;
    stock: number;
    sku_sales_count: number;
    specs: Record<string, string>;
    image_url?: string | null;
  }>;
};

export type OrderItemCard = {
  id: number;
  sku_id: number;
  sku_name: string;
  sku_specs?: Record<string, unknown> | null;
  price: string;
  quantity: number;
};

export type OrderCard = {
  id: number;
  status: number;
  status_label: string;
  total_amount: string;
  pay_amount: string;
  freight_amount: string;
  created_at: string;
  pay_at?: string | null;
  delivery_at?: string | null;
  items: OrderItemCard[];
  logistics?: {
    express_company?: string | null;
    logistic_no?: string | null;
    status: number;
    trace: Array<Record<string, string>>;
  } | null;
};

export type OrderSummary = {
  id: number;
  status: number;
  status_label: string;
  total_amount: string;
  pay_amount: string;
  created_at: string;
  item_count: number;
  first_item_name?: string | null;
  express_company?: string | null;
  logistic_no?: string | null;
  latest_logistics_event?: Record<string, unknown> | null;
};

export type OrderQueryMeta = {
  query_mode:
    | "explicit"
    | "latest"
    | "recent"
    | "all"
    | "remaining"
    | "count"
    | "page"
    | "analysis";
  total_match_count: number;
  returned_count: number;
  is_exhaustive: boolean;
  offset: number;
  next_offset?: number | null;
};

export type SuggestedAction = {
  label: string;
  payload: Record<string, unknown>;
};

export type MemoryItem = {
  id: number;
  key: string;
  fact_type: string;
  display_value: string;
  structured_value: Record<string, unknown>;
  origin: string;
  created_at: string;
  updated_at: string;
  last_used_at?: string | null;
};

export type MemoryChange = {
  action: "created" | "updated";
  memory_id: number;
  key: string;
  display_value: string;
};

export type AuthUser = {
  id: number;
  login_identifier: string;
  display_name: string;
  status: string;
  last_login_at?: string | null;
};

export type AuthTokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUser;
};

export type AuthSession = {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  user: AuthUser;
};

export type BoundaryClassificationValue =
  | "in_scope_auto"
  | "human_handoff_required"
  | "out_of_scope"
  | "security_refusal";

export type BoundaryClassification = {
  classification: BoundaryClassificationValue;
  reason: string;
  display_message: string;
};

export type EvidenceItem = {
  source_type: "knowledge_document";
  source_id: number;
  title: string;
  document_type: string;
  snippet: string;
  score?: number | null;
  metadata: Record<string, unknown>;
};

export type ChatResponse = {
  conversation_id: number;
  answer: string;
  intent: string;
  boundary: BoundaryClassification;
  evidence: EvidenceItem[];
  products: ProductCard[];
  order?: OrderCard | null;
  orders: OrderSummary[];
  order_query?: OrderQueryMeta | null;
  suggested_actions: SuggestedAction[];
  memory_changes?: MemoryChange[] | null;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  status?: "sent" | "failed" | "received" | "pending" | "streaming" | "cancelled";
  streamStage?: string;
  boundary?: BoundaryClassification;
  intent?: string;
  evidenceCount?: number;
  productCount?: number;
  orderId?: number;
  suggestedActions?: SuggestedAction[];
  products?: ProductCard[];
};

export type AfterSalesTicket = {
  id: number;
  order_id: number;
  order_item_id: number;
  ticket_type: string;
  reason: string;
  status: string;
  created_at: string;
};

export type HandoffRequestType = "refund" | "return" | "repair" | "order_change" | "other";

export type HandoffRequestStatus = "pending" | "acknowledged" | "resolved";

export type HandoffRequestAccepted = {
  request_id: number;
  status: HandoffRequestStatus;
  message: string;
};

export type HandoffRequest = {
  id: number;
  session_id: number;
  order_id?: number | null;
  request_type: HandoffRequestType;
  reason: string;
  boundary_category: string;
  status: HandoffRequestStatus;
  created_at: string;
  updated_at: string;
};

export type ResponseStatus =
  | "ready"
  | "loading"
  | "streaming"
  | "success"
  | "handoff"
  | "blocked"
  | "error"
  | "cancelled";

export type OperatorProfile = {
  name: string;
  role: string;
  userId: number;
  loginIdentifier: string;
  authState: "authenticated";
  statusLabel: string;
};

export type PendingRequest = {
  message: string;
  conversationId?: number;
  messageId?: string;
};

export type RequestError = {
  message: string;
  retryable: boolean;
  status?: number;
  request?: PendingRequest;
};

export type ConversationSummary = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
  last_message?: string | null;
  last_message_role?: "user" | "assistant" | null;
  last_message_at?: string | null;
};

export type ConversationMessageItem = {
  id: number;
  role: "user" | "assistant";
  content: string;
  metadata?: Record<string, unknown> | null;
  created_at: string;
};

export type ConversationDetail = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
  messages: ConversationMessageItem[];
};

export type ChatStreamEvent =
  | {
      type: "run_started";
      conversation_id: number;
      run_id: number;
    }
  | {
      type: "boundary";
      conversation_id: number;
      run_id: number;
      boundary: BoundaryClassification;
    }
  | {
      type: "tool_call";
      conversation_id: number;
      run_id: number;
      tool_name: string;
      status: "started" | "completed" | "error";
      input?: Record<string, unknown>;
      output?: Record<string, unknown>;
    }
  | {
      type: "context";
      conversation_id: number;
      run_id: number;
      intent?: string;
      boundary?: BoundaryClassification;
      evidence: EvidenceItem[];
      products: ProductCard[];
      order?: OrderCard | null;
    }
  | {
      type: "delta";
      conversation_id: number;
      run_id: number;
      delta: string;
    }
  | {
      type: "done";
      conversation_id: number;
      run_id: number;
      response: ChatResponse;
    }
  | {
      type: "error";
      conversation_id?: number;
      run_id?: number;
      error_type?: string;
      message: string;
      retryable?: boolean;
    };

export type ConversationTurn = {
  id: string;
  userMessage: string;
  assistantAnswer: string;
  intent: string;
  boundary: BoundaryClassification;
  evidenceCount: number;
  productCount: number;
  orderId?: number;
  suggestedActions: SuggestedAction[];
  createdAt: string;
};

export type HandoffNotice = {
  requested: boolean;
  source: string;
  reason: string;
  orderId?: number;
  requestId?: number;
  status?: HandoffRequestStatus;
  message?: string;
  updatedAt: string;
};
