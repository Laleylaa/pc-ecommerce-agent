# Agent loop 测试

流程与隔离要求见 [tests/README.md](../README.md)；结果复检见 [tests/REVIEW.md](../REVIEW.md)。

## 分类与入口

| 目录 | 验证范围 |
| --- | --- |
| `smoke/` | 正式 runtime / SDK / 工具绑定 / 回答阶段的最小代表链路 |
| `contracts/` | 准入、批次回滚、错误恢复、证据、预算、取消及追踪边界 |
| `business/` | 用户目标及业务结果；与冒烟、契约迁移分别验收 |
| `support/` | 合成数据与替身，不互相导入测试函数 |

从项目根执行冒烟与契约测试：

```bash
backend/.venv/bin/python tests/run.py tests/agent_loop/smoke tests/agent_loop/contracts -q
```

该入口阻断网络、真实数据库和凭据；不运行真实模型、Phoenix演示或聊天端到端。
`tests/trace_demo.py` 是显式本地追踪演示，复用 `support/trace_scenario.py`，不随上述命令执行。

## 当前断言口径

- 正式业务工具失败检查 `ok=false、output=null、error.code、error.next_action`，不要求旧的公开错误字段或内部异常详情。
- 普通工具查询超时可返回 `dependency_unavailable/retry`，同一操作最多额外重试一次；一般依赖故障及内部错误返回 `stop`，不能重放。用相应故障替身验证两种路径，不用 `OSError` 冒充允许重试的超时。
- 工具交付/观察投影超限为 `resource_limit/narrow_request`；取消、执行截止为 `resource_limit/stop`。取消继续传播，并验证清理和失败证据。
- 工具规划失败不能作为成功事实交付。无匹配、资料不足、不支持等正常领域结果继续保留自己的语义，不能一律改成外层失败。
- `AdmissionError`、控制工具拒绝、内部审计事件和运行生命周期错误不属于公开业务工具错误契约。其 `invalid_input`、`deadline_exhausted`、`cancelled` 等内部标识按各自接口验证，不机械替换。
- 首批回滚检查目标、操作、尝试、绑定均无残留，再检查修正调用能够执行；不访问已删除的运行时属性。
- 追踪场景为一次参数拒绝、一次查询超时、一次同参数恢复成功；验证证据替代及关联，不通过换参数绕过重试上限。

离线通过只说明合成条件下的确定性契约成立，不代表真实语义效果、业务测试或完整聊天已验收。
