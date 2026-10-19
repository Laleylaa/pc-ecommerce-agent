# 后端测试

按业务模块组织；基础业务、契约、边界和异常在对应模块内维护，不另建 unit/contracts 层。

| 目录 | 范围 |
| --- | --- |
| catalog | 正式商品搜索、目录、规格、商品 repository 及评测器 |
| orders | 订单查询、字段能力、身份选择和过滤 |
| knowledge | 政策、本地检索、Web 搜索 |
| orchestration | 规划、绑定、执行、结果交接、回答、重试/恢复/追踪 |
| memory | 上下文、工作记忆、长期记忆 |
| auth | 认证和跨用户隔离 |
| api | HTTP/SSE、输入和记忆接口 |
| infrastructure | 配置、LLM 基础设施、导入和种子脚本 |
| support | 共享模型/数据/上下文替身；不放测试函数 |
| e2e | 完整后端业务链路；当前尚无固定自动化冒烟套件 |

编排组合场景由 `test_goal_task_integration.py`、`test_planning_handoff.py` 等当前入口测试维护。
模块测试中的 `smoke` 标记仅挑选已有代表性业务检查，不等同完整应用冒烟。
真实模型评测仍在 `backend/evals/`，入口在 `backend/scripts/`。

## 运行

从 `backend/` 执行：

```sh
# 当前不含真实数据库测试，默认排除 network 标记测试
.venv/bin/python -m pytest tests -q
# 推荐的隔离回归：额外阻断 socket 与 AsyncEngine.connect
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests -q
# 按模块运行，或执行已选定的模块冒烟
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests/catalog -q
.venv/bin/python -m pytest -p scripts.orchestration_audit_safety -m smoke -q
```

需要网络的测试标记 `network`，显式 `--run-network` 才允许进入执行集合。
当前自动化测试使用替身，不含连接配置数据库的集成测试；`--run-db` 与 `db` 标记已删除。
安全插件继续阻断 socket 与 AsyncEngine.connect，作为额外保护，不能替代 fixture 审查。
当前已选模块冒烟使用替身，无真实模型；评测脚本仍可能访问模型或数据库，应单独审查。

## 本次提交的验证边界

2026-10-19收尾提交补齐执行详情实现、前端接入及SSE退役后的普通聊天评测入口，
解决此前测试提交缺少`app.agent.execution_details`的依赖问题。
首次完整隔离回归为1817通过、8失败：反馈测试未适配已接入的语义审核、两次纠错预算及新增trace事件。
本次仅调整这些测试的模型替身与断言，保留原文、错误分类和预算耗尽检查，不改业务实现。
历史1764项成绩属于当时完整工作区，不代表此前测试提交独立检出通过。

修正后完整隔离回归：**1825通过，0失败，0跳过，0排除**（22.40秒）。
运行命令：`.venv/bin/python -m pytest -p scripts.orchestration_audit_safety tests -q`。
改动文件Ruff、前端TypeScript/Vite构建和diff检查通过；未运行真实模型、数据库评测或浏览器验收。

## 当前状态（2026-10-18）

按用户决定移除30项真实数据库用例（28个测试函数），删除专用数据库fixture及执行开关。
普通聊天、权限和订单归属的替身测试、业务规则及查询构造检查继续保留。

最终隔离回归：**1764通过，0失败，0跳过，0排除**。生产代码和数据库未修改，未提交。
详见[数据库测试删除记录](../evals/test_maintenance/db-test-removal-20261018.json)。
这表示保留的隔离集合通过，不验证真实数据库读写、事务、SQL执行或跨用户持久化隔离。
完整应用E2E冒烟仍未建立，后续按功能变化补充必要测试。

历史记录：
[SSE退役](../evals/test_maintenance/sse-retirement-20261018.json)、
[目录迁移与首轮删除](../evals/test_maintenance/cleanup-20261018.json)、
[第二轮删除](../evals/test_maintenance/pruning-20261018.json)、
[第三轮清理](../evals/test_maintenance/final-pruning-20261018.json)。
