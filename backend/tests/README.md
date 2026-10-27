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
| api | HTTP 聊天、输入和记忆接口 |
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

## 结果保存

开发问题、迭代记录与测试成绩只保存在本地 `docs/`、`artifacts/` 或忽略的评测输出目录。
本页维护测试入口与适用范围，不记录具体批次结果。隔离测试不代表真实数据库或完整聊天验收。
