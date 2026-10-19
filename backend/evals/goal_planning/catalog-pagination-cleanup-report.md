# 商品回答分页口径清理

- 日期：2026-10-01；`feat/orchestrator-catalog-integration`，基点 `744cc0f` 加工作区修改。
- 范围：修复 TI-03 中的商品回答分页残留；知识检索意图问题按用户要求暂不处理。
- 结果：8个真实模型场景的分页边界复核通过；隔离回归256 passed、1 xfailed（GI-02）。不代表全部回答语义通过，数量解释问题见下文。

## 修改

- `answer_context.py` 删除按 next_offset 说明分页的指令；回答模型中的商品事实和 result_window 不再携带 offset/next_offset，query_plan 不再带 offset。只投影副本，不修改运行产物或记忆；数量、匹配状态及事实保留。
- `prompts/static.py` 统一带工具结果的最终回答边界：只支持候选数量或条件调整后重新查询，不承诺跳页、接着上批查询或自动去重。覆盖查询与分页能力询问混合的请求。
- `responses.py` 保底文案从“本页／分页候选”改为“本次返回的候选”；不改变工具内部窗口、身份绑定或订单续查能力。

## 验证

使用已有人工商品仓库、正式工具实现及配置模型 `deepseek-flash`，8例均运行完整图至 ChatResponse；无真实数据库、HTTP/SSE、会话持久化或浏览器页面操作。自动结构检查8/8通过，再逐例人工审核分页承诺及结果解释。

| 固定用例 | 本轮分页/执行检查 |
| --- | --- |
| products_two：原失败例 | 两张卡片、limit=2，说明4款中本次返回2款；建议调整数量/条件重新查询，未承诺翻页 |
| products_default：默认数量 | 省略limit，返回3款；未建议翻页，但出现下述数量归因错误 |
| products_fewer：要两款粉色键盘 | 只找到1款并如实说明不足两款；没有伪造第二款或提出下一页 |
| product_absent：查SKU999价格 | 说明未定位到商品；无卡片、无翻页建议 |
| catalog_failure：依赖故障 | 正式绑定捕获OSError，工具只调用一次；说明查询暂时失败，无商品卡片 |
| mixed_page_request：查询两款＋询问跳页 | 返回两款，同时解释不能跳到第三页 |
| explicit_page：跳到第三页 | 不调用商品工具，说明不支持跳页 |
| exclude_previous：往后翻且排除已展示 | 不调用商品工具，明确不支持翻页或自动排除上批 |

原始计划、工具结果、回答及当时源码指纹：[8例输出](catalog-pagination-cleanup-results.json)。随后仅调整了两处保底文案；上述真实模型场景的执行/模型输入未再修改。

隔离测试在[前轮安全命令](contract-alignment-report.md)的选择范围上增加 `tests/test_answer_context.py` 和 `tests/test_orchestrator_outcomes.py`，fixture闭包排除写配置数据库的用例；共257例，256通过、1已知xfail。新增断言覆盖零/少量/部分候选的游标投影、计数保留、原始产物不变，以及订单续查不受影响。相关5个Python文件Ruff通过。

## 独立发现与未覆盖范围

`products_default` 返回 total_match_count=4、candidate_count=4、returned_count=3、unknown_sku_count=1。回答把尚未展示的第4款严格匹配商品说成“资料暂时无法确认”，混淆了 limit 截断与另一个资料不足的SKU。这是回答事实解释问题，已补充到 RV-01；本轮未顺带修复，不将该例称为完整业务回答通过。

TI-03的分页残留已处理；剩余找知识资料意图误分仍保留并暂缓。真实页面展示、数据库/检索索引、持久化与广泛表达稳定性没有验收，不升级模块稳定基线。

从backend复跑（付费模型、人工数据，不覆盖历史产物）：

```sh
.venv/bin/python -m scripts.eval_contract_acceptance --live \
  --case products_two --case products_default --case products_fewer \
  --case product_absent --case catalog_failure --case mixed_page_request \
  --case explicit_page --case exclude_previous \
  --output /private/tmp/catalog-pagination-rerun.json
```
