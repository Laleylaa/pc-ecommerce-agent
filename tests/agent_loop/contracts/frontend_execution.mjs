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
// single order_query. Successful empty results must clear stale cards; failures
// and non-order turns must not silently erase the previous order selection.
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
  let order = { id: 8002 }, orders = [{ id: 8002 }], orderQuery = { total_match_count: 99 };
  const noop = () => {};
  const setConversationId = noop, setBoundary = noop, setEvidence = noop, setProducts = noop;
  const setSuggestedActions = noop, updateMessage = noop, setTurns = noop;
  const setHandoffNotice = noop, setResponseStatus = noop;
  const statusForBoundary = () => "ready";
  const isBlockedBoundary = b => b.classification === "security_refusal";
  const setOrder = f => { order = f(order); };
  const setOrders = value => { orders = value; };
  const setOrderQuery = value => { orderQuery = value; };
  ${responseCode}
  return { apply: applyFinalResponse, state: () => ({ order, orders, orderQuery }) };
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
  assert.deepEqual(ui.state(), successful
    ? { order: null, orders: results, orderQuery: null }
    : { order: { id: 8002 }, orders: [{ id: 8002 }], orderQuery: { total_match_count: 99 } });
}
const oldConsumer = makeResponseConsumer();
oldConsumer.apply({
  conversation_id: 31, answer: "旧历史", boundary: { classification: "in_scope_auto" },
  evidence: [], products: [], orders: [{ id: 8201 }], order: { id: 8201 },
  order_query: { total_match_count: 1 }, suggested_actions: []
}, "message", "旧单查询");
assert.deepEqual(oldConsumer.state(), {
  order: { id: 8201 }, orders: [{ id: 8201 }], orderQuery: { total_match_count: 1 }
});
