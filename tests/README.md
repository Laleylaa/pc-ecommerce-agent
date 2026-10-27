# 开发测试

测试代码、固定输入、独立预期和复跑说明保存在 develop；实际批次、成绩、排错过程和 issues
只保存在本地 docs、artifacts 或已忽略的 results。输入清单不表示对应场景已经通过。

## 离线入口

先使用 `make dev-install` 准备开发依赖，再从仓库根执行：

```bash
make check
make agent-loop-test
make tools-test
make application-test
```

默认 `tests/run.py` 阻断网络、真实数据库和真实凭据读取。单个模块可这样选择：

```bash
backend/.venv/bin/python tests/run.py tests/tools/catalog_lookup -q
```

| 目录 | 职责 |
| --- | --- |
| `agent_loop/` | 当前编排的业务、冒烟、契约及故障边界 |
| `tools/` | 各公开工具与共享规则 |
| `application/` | API、认证、记忆、配置与启动入口 |
| `support/` | 隔离保护、合成数据和共享替身 |
| `e2e/` | 显式聊天 API 端到端装置，见该目录 README |
| `integration/` | 显式真实数据库检查，不属于默认集合 |

## 真实评测与证据

真实模型、数据库、HTTP 和 Phoenix 演示不随默认测试执行。运行前核对费用、目标数据库、
读写影响和用户授权；不能把隔离测试通过等同于真实系统验收。
固定用例应包含独立的预期和数据前提，程序检查与语义复检分别记录，不能用内部 approve
代替独立判定。方法见 [复检手册](REVIEW.md)。

`coverage.json` 仅描述场景和输入分布；输出写到 artifacts 或相应 results。
真实批次冻结源码和输入指纹，代码/说明变化后应新建批次，不覆盖或重新解释旧结果。
复检说明可以随代码提供，个人开发进度与验收流水只写本地资料。
