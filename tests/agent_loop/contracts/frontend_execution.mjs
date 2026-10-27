// Actual TypeScript functions + React server render; browser acceptance is separate.
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const frontend = path.resolve("frontend");
const require = createRequire(path.join(frontend, "package.json"));
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const configPath = path.join(frontend, "tsconfig.json");
const config = ts.readConfigFile(configPath, ts.sys.readFile);
assert.equal(config.error, undefined);
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, frontend, {}, configPath);
const program = ts.createProgram(parsed.fileNames, parsed.options);
const diagnostics = [...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
assert.equal(diagnostics.length, 0, ts.formatDiagnosticsWithColorAndContext(diagnostics, {
  getCanonicalFileName: name => name,
  getCurrentDirectory: () => process.cwd(),
  getNewLine: () => "\n"
}));
function load(relative) {
  const source = fs.readFileSync(path.join(frontend, relative), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  });
  const exports = {};
  new Function("require", "exports", outputText)(require, exports);
  return exports;
}
const { executionDetailsFromMetadata: normalize } = load("src/utils/execution-details.ts");
const { TurnExecution } = load("src/components/TurnExecution.tsx");
assert.equal(normalize(null), undefined);
const legacy = normalize({ goals: [{ id: "old", query: "旧问题" }], tool_calls: [{ id: "old-call" }] });
assert.deepEqual(legacy.goals[0].task_ids, []);
assert.deepEqual(legacy.tool_calls[0].arguments, {});
assert.equal(legacy.model_calls, null);
assert.match(renderToStaticMarkup(React.createElement(TurnExecution, { details: legacy })), /旧问题/);
const current = normalize({
  goals: [{ id: "g1", query: "新问题", disposition: "tool_planning", task_ids: ["op1"] }],
  tool_calls: [
    { id: "a1", task_id: "op1", tool_name: "catalog_search", outcome: "error", superseded: true },
    { id: "a2", task_id: "op1", tool_name: "catalog_search", outcome: "usable", used_in_answer: true }
  ], model_calls: { query: 4, answer: 1, invalid: -1 }
});
const html = renderToStaticMarkup(React.createElement(TurnExecution, { details: current }));
for (const text of ["2 次工具调用", "模型 5 次", "查询决策", "已被替代", "回答已引用", "调用失败"])
  assert.ok(html.includes(text), text);
assert.ok(!html.includes("其他调用 / 恢复前记录"));

// Execute the actual App handlers with isolated state/API, not copied handler logic.
// This checks confirmation/submission contracts; it does not simulate browser clicks.
const appSource = ts.createSourceFile("App.tsx",
  fs.readFileSync(path.join(frontend, "src/App.tsx"), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["handleSuggestedAction", "handleRequestHandoff", "numberFromPayload"]);
const handlers = [];
function collect(node) {
  if (ts.isFunctionDeclaration(node) && names.has(node.name?.text)) handlers.push(node.getText(appSource));
  ts.forEachChild(node, collect);
}
collect(appSource);
assert.equal(handlers.length, 3);
const handlerCode = ts.transpileModule(handlers.join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const makeHandlers = new Function("createHandoffRequest", "order", `
  let handoffNotice = null;
  const conversationId = 31, ticketReason = "用户确认的退货原因", ticketType = "return";
  const boundary = { reason: "需要人工确认" };
  const setHandoffNotice = value => { handoffNotice = value; };
  const noop = () => {};
  const setResponseStatus = noop, setSuggestedActions = noop, setHandoffSubmitting = noop;
  const setHandoffQueryError = noop, setHandoffQueryId = noop, setHandoffQueryResult = noop;
  const setError = value => { if (value) throw new Error("unexpected submission error"); };
  const toRequestError = err => { throw err; };
  ${handlerCode}
  return { suggest: handleSuggestedAction, submit: handleRequestHandoff,
    notice: () => handoffNotice };
`);
for (const previousOrder of [undefined, { id: 8002 }]) {
  const submissions = [];
  const ui = makeHandlers(async body => {
    submissions.push(body);
    return { request_id: 91, status: "pending", message: "已记录，尚未接单" };
  }, previousOrder);
  ui.suggest({ label: "转人工客服", payload: { handoff: true, orderId: 7001 } });
  assert.equal(ui.notice().orderId, 7001);
  assert.equal(submissions.length, 0, "showing confirmation must not submit");
  await ui.submit();
  assert.deepEqual(submissions, [{ session_id: 31, order_id: 7001,
    request_type: "return", reason: "用户确认的退货原因" }]);
  assert.equal(ui.notice().orderId, 7001);
  assert.equal(ui.notice().requestId, 91);
  await ui.submit();
  assert.equal(submissions.length, 1, "already recorded request must not submit twice");
}
console.log("frontend execution/history contracts passed");

// Run the actual response consumer: multi-query responses deliberately have no
// single order_query. Each message owns only its returned cards; the selected
// order remains separate for the explicit handoff confirmation.
let responseHandler;
function findResponseHandler(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "applyFinalResponse")
    responseHandler = node.getText(appSource);
  ts.forEachChild(node, findResponseHandler);
}
findResponseHandler(appSource);
assert.ok(responseHandler);
const responseCode = ts.transpileModule(responseHandler, {
  compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const makeResponseConsumer = new Function(`
  let order = { id: 8002 }, message = {};
  const noop = () => {};
  const setConversationId = noop, setBoundary = noop, setEvidence = noop, setProducts = noop;
  const setSuggestedActions = noop;
  const updateMessage = (_id, update) => { message = update(message); };
  const setHandoffNotice = noop, setResponseStatus = noop;
  const statusForBoundary = () => "ready";
  const isBlockedBoundary = b => b.classification === "security_refusal";
  const setOrder = f => { order = f(order); };
  ${responseCode}
  return { apply: applyFinalResponse, state: () => ({ order, message }) };
`);
for (const outcome of ["usable", "empty", "not_found", "error", null]) {
  const ui = makeResponseConsumer();
  const successful = ["usable", "empty", "not_found"].includes(outcome);
  const results = outcome === "usable" ? [{ id: 8201 }, { id: 8202 }] : [];
  ui.apply({
    conversation_id: 31, answer: "结果", boundary: { classification: "in_scope_auto" },
    evidence: [], products: [], orders: results, order: null, order_query: null,
    suggested_actions: [],
    execution_details: { tool_calls: outcome ? [{ tool_name: "order_lookup", outcome }] : [] }
  }, "message", "查订单");
  assert.deepEqual(ui.state().order, successful ? null : { id: 8002 });
  assert.deepEqual(ui.state().message.orders, results, "message results never inherit old orders");
  assert.equal(ui.state().message.order, null);

}
const oldConsumer = makeResponseConsumer();
oldConsumer.apply({
  conversation_id: 31, answer: "旧历史", boundary: { classification: "in_scope_auto" },
  evidence: [], products: [], orders: [{ id: 8201 }], order: { id: 8201 },
  order_query: { total_match_count: 1 }, suggested_actions: []
}, "message", "旧单查询");
assert.deepEqual(oldConsumer.state().order, { id: 8201 });
assert.deepEqual(oldConsumer.state().message.order, { id: 8201 });
assert.deepEqual(oldConsumer.state().message.orders, [{ id: 8201 }]);
assert.deepEqual(oldConsumer.state().message.orderQuery, { total_match_count: 1 });

// Reloading a conversation must preserve each answer's business data, not only
// the last answer's sidebar snapshot. Blocked answers cannot expose stale cards.
const historyNames = new Set(["messageFromHistory", "boundaryFromMetadata", "isBlockedBoundary",
  "listFromMetadata", "orderFromMetadata", "orderQueryFromMetadata", "isRecord"]);
const historyFunctions = [];
function collectHistory(node) {
  if (ts.isFunctionDeclaration(node) && historyNames.has(node.name?.text)) historyFunctions.push(node.getText(appSource));
  ts.forEachChild(node, collectHistory);
}
collectHistory(appSource);
const historyCode = ts.transpileModule(historyFunctions.join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 }
}).outputText;
const restore = new Function(`${historyCode}; return messageFromHistory;`)();
const saved = { id: 20, role: "assistant", content: "查询结果", created_at: "2026-10-26T10:00:00Z",
  metadata: { products: [{ sku_id: 11 }], order: { id: 31 }, orders: [{ id: 32 }],
    evidence: [{ title: "政策资料" }], order_query: { query_mode: "recent", total_match_count: 2, returned_count: 2 },
    boundary: { classification: "in_scope_auto" } } };
const restored = restore(saved);
assert.deepEqual(restored.products, [{ sku_id: 11 }]);
assert.deepEqual(restored.order, { id: 31 });
assert.deepEqual(restored.orders, [{ id: 32 }]);
assert.deepEqual(restored.evidence, [{ title: "政策资料" }]);
assert.equal(restore({ ...saved, metadata: {} }).orders.length, 0);
const blocked = restore({ ...saved, metadata: { ...saved.metadata, boundary: { classification: "security_refusal" } } });
for (const key of ["products", "orders", "evidence"]) assert.deepEqual(blocked[key], []);
assert.equal(blocked.order, null);
console.log("message-scoped results and history restoration contracts passed");

// Render real result components with synthetic data, no browser or API calls.
function loadComponent(relative) {
  const source = fs.readFileSync(path.join(frontend, relative), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 }
  });
  const exports = {};
  const localRequire = name => {
    if (!name.startsWith(".")) return require(name);
    const base = path.resolve(frontend, path.dirname(relative), name);
    const file = [base + ".tsx", base + ".ts"].find(candidate => fs.existsSync(candidate));
    return loadComponent(path.relative(frontend, file));
  };
  new Function("require", "exports", outputText)(localRequire, exports);
  return exports;
}
const { ProductCardRow, productPresentation } = loadComponent("src/components/ProductInlineCard.tsx");
const { formatSpec } = loadComponent("src/utils/spec-display.ts");
const displayCases = JSON.parse(fs.readFileSync("tests/tools/shared/spec_display_cases.json", "utf8"));
for (const item of displayCases) {
  const formatted = formatSpec(item.key, item.value, item.category);
  assert.equal(formatted.label, item.label);
  assert.equal(formatted.value, item.expected, item.key);
}
const product = { spu_id: 1, sku_id: 2, title: "样本版本", spu_title: "示例键盘", entity_scope: "spu",
  brand: "示例", category: "键盘", price: "199", stock: 10, specs: { 轴体: "样本独有轴体" },
  series_common_specs: { 连接方式: "无线" }, series_min_price: "199", series_max_price: "399" };
const cardHtml = renderToStaticMarkup(React.createElement(ProductCardRow, { products: [product] }));
assert.ok(cardHtml.includes("示例键盘"));
assert.ok(cardHtml.includes("¥199–399"));
assert.ok(!cardHtml.includes("样本独有轴体"), "a representative SKU must not become a series-wide fact");
assert.equal(productPresentation({ ...product, entity_scope: "sku" }).price, "¥199");
const { ProductDetail } = loadComponent("src/components/ProductDetail.tsx");
const mappedSeries = { ...product, category: "显示器", series_common_specs: { refresh_rate: "144" },
  series_option_specs: { size_inch: ["24", "27"] } };
const mappedDetail = renderToStaticMarkup(React.createElement(ProductDetail, { product: mappedSeries, onAsk: () => {} }));
for (const text of ["刷新率", "144 Hz", "屏幕尺寸", "24 英寸 / 27 英寸"]) assert.ok(mappedDetail.includes(text));
assert.ok(!mappedDetail.includes("样本独有轴体"));
const mappedPreview = renderToStaticMarkup(React.createElement(ProductCardRow, { products: [mappedSeries] }));
assert.ok(mappedPreview.includes("刷新率：144 Hz"));
const { MessageResults } = loadComponent("src/components/MessageResults.tsx");
const resultHtml = renderToStaticMarkup(React.createElement(MessageResults, { message: {
  id: "answer", role: "assistant", createdAt: saved.created_at, content: "",
  orders: [{ id: 31, status_label: "已发货", first_item_name: "示例键盘", pay_amount: "199", created_at: saved.created_at, item_count: 1 }],
  evidence: [{ source_type: "knowledge_document", source_id: 1, title: "退货政策", snippet: "示例条款", score: 0.99, metadata: { internal: "hidden" } }]
} }));
for (const text of ["已发货", "示例键盘", "退货政策", "示例条款"]) assert.ok(resultHtml.includes(text), text);
assert.ok(!resultHtml.includes("0.99") && !resultHtml.includes("hidden"));
console.log("product/order/source rendering contracts passed");

// Order presentation preserves facts/units and determines chronology from timestamps.
const { OrderCardView, OrderDetailContent, orderLogisticsEvents, formatSkuSpecs } = loadComponent("src/components/OrderCards.tsx");
assert.equal(formatSkuSpecs({ color: "黑色", max_dpi: 16000, weight_g: 58 }), "颜色：黑色 · 最高 DPI：16000 · 重量：58 g");
for (const weight of ["58 g", "0.058 kg", "约 58 克", "未提供"])
  assert.equal(formatSkuSpecs({ weight_g: weight }), `重量：${weight}`);
assert.equal(formatSkuSpecs({ weight_g: "58" }), "重量：58 g");
assert.equal(formatSkuSpecs({ weight_g: null, unknown_field: "原始值" }), "重量：未标注 · unknown_field：原始值");
const traceNewest = { time: "2026-10-26 09:20", text: "包裹运输中" };
const traceOldest = { time: "2026-10-25 16:00", text: "包裹已揽收" };
for (const trace of [[traceOldest, traceNewest], [traceNewest, traceOldest]]) {
  const before = JSON.stringify(trace);
  assert.deepEqual(orderLogisticsEvents(trace), { events: [traceNewest, traceOldest], latestKnown: true });
  assert.equal(JSON.stringify(trace), before, "display sorting must not mutate saved evidence");
}
const missingTime = { text: "时间未知的记录" };
assert.deepEqual(orderLogisticsEvents([missingTime, traceOldest]), { events: [missingTime, traceOldest], latestKnown: false });
assert.equal(orderLogisticsEvents([{ time: "无效时间", text: "记录" }]).latestKnown, false);
const detailedOrder = { id: 8001, status_label: "已发货", total_amount: "129", pay_amount: "129", freight_amount: "0", created_at: saved.created_at,
  items: [{ id: 1, sku_name: "示例鼠标", sku_specs: { color: "黑色", max_dpi: 16000, weight_g: 58 }, price: "129", quantity: 1 }],
  logistics: { express_company: "示例快递", logistic_no: "DEMO8001", trace: [traceNewest, traceOldest] } };
const compactOrderHtml = renderToStaticMarkup(React.createElement(OrderCardView, { order: detailedOrder, onOpen: () => {} }));
for (const text of ["示例鼠标", "¥129", "包裹运输中", "查看订单与物流"]) assert.ok(compactOrderHtml.includes(text));
assert.ok(!compactOrderHtml.includes("<details") && !compactOrderHtml.includes("重量："), "chat preview does not embed full details");
const manyOrdersHtml = renderToStaticMarkup(React.createElement(MessageResults, { message: {
  id: "many-orders", createdAt: saved.created_at, order: detailedOrder,
  orders: [8001, 8002, 8003, 8004].map(id => ({ id, status_label: "已发货", pay_amount: "129", created_at: saved.created_at, first_item_name: `商品${id}`, item_count: 1 }))
} }));
assert.equal((manyOrdersHtml.match(/class="order-box order-compact"/g) ?? []).length, 2);
assert.ok(manyOrdersHtml.includes("查看本次全部 4 笔"));
assert.ok(!manyOrdersHtml.includes("商品8003"));
const renderOrder = order => renderToStaticMarkup(React.createElement(OrderDetailContent, { order }));
const detailedHtml = renderOrder(detailedOrder);
for (const text of ["订单详情", "物流进度", "最新动态", "包裹运输中", "颜色：黑色", "最高 DPI：16000", "重量：58 g"])
  assert.ok(detailedHtml.includes(text), text);
assert.equal((detailedHtml.match(/<details/g) ?? []).length, 2, "details and timeline expand independently");
assert.ok(!/<details[^>]*\bopen/.test(detailedHtml), "secondary details start collapsed");
assert.ok(renderOrder({ ...detailedOrder, logistics: null }).includes("暂无物流轨迹"));
const uncertainHtml = renderOrder({ ...detailedOrder, logistics: { trace: [missingTime, traceOldest] } });
assert.ok(!uncertainHtml.includes("最新动态"));
assert.ok(uncertainHtml.includes("时间未知的记录"));
console.log("order presentation and chronology contracts passed");

// A long result set has a bounded preview; the drawer still exposes every item.
const fiveProducts = Array.from({ length: 5 }, (_, index) => ({ ...product,
  sku_id: index + 10, spu_id: index + 10, spu_title: `候选键盘${index + 1}` }));
const previewHtml = renderToStaticMarkup(React.createElement(ProductCardRow, {
  products: fiveProducts, onViewAll: () => {}, onProductClick: () => {}
}));
assert.equal((previewHtml.match(/class="product-inline-card"/g) ?? []).length, 2);
assert.ok(previewHtml.includes("查看全部 5 款"));
assert.ok(!previewHtml.includes("候选键盘3"));
const { ProductResultsDrawer } = loadComponent("src/components/ProductResultsDrawer.tsx");
const drawerHtml = renderToStaticMarkup(React.createElement(ProductResultsDrawer, {
  products: fiveProducts, onClose: () => {}, onAsk: () => {}
}));
for (let i = 1; i <= 5; i++) assert.ok(drawerHtml.includes(`候选键盘${i}`));

// Execute the actual scroll decision and geometry against controlled DOM sizes.
// No browser/DOM library or model call: these guard response completion vs reading history.
const chatSource = ts.createSourceFile("ChatPanel.tsx",
  fs.readFileSync(path.join(frontend, "src/components/ChatPanel.tsx"), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let revealSource, scrollEffect;
function findScroll(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "revealMessage") revealSource = node.getText(chatSource);
  if (ts.isCallExpression(node) && node.expression.getText(chatSource) === "useLayoutEffect")
    scrollEffect = node.arguments[0].getText(chatSource);
  ts.forEachChild(node, findScroll);
}
findScroll(chatSource);
const js = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const revealFactory = new Function("messagesRef", "nearBottom", js(revealSource) + "; return revealMessage;");
for (const [height, expected] of [[900, 384], [120, 44]]) {
  const container = { scrollTop: 100, scrollHeight: 2000, clientHeight: 500,
    getBoundingClientRect: () => ({ top: 80 }),
    querySelectorAll: () => [{ dataset: { messageId: "answer" },
      getBoundingClientRect: () => ({ top: 380, height }) }] };
  revealFactory({ current: container }, { current: true })("answer");
  assert.equal(container.scrollTop, expected, "long reply starts at heading; short reply fits entirely");
}
const decide = new Function("messages", "previousLast", "nearBottom", "revealMessage", "setUnreadReply",
  js(`const effect = ${scrollEffect}; effect();`));
const pending = { id: "answer", role: "assistant", status: "pending" };
const ready = { ...pending, status: "complete" };
for (const near of [true, false]) {
  const revealed = [], unread = [];
  decide([ready], { current: pending }, { current: near }, id => revealed.push(id), id => unread.push(id));
  assert.deepEqual(revealed, near ? ["answer"] : [], "reading history must not be interrupted");
  assert.deepEqual(unread, [near ? null : "answer"]);
}
const idleReveals = [];
decide([ready], { current: ready }, { current: true }, id => idleReveals.push(id), () => {});
assert.deepEqual(idleReveals, [], "unrelated updates cannot reset the reading position");
console.log("compact product preview and reply scrolling contracts passed");
