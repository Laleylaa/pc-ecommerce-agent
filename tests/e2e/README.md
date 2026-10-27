# 聊天 API 端到端验证

本装置经过真实聊天 API、会话上下文、工具、模型和数据库，再核对历史正文与卡片。
不包含浏览器渲染、登录表单和真实支付/退款办理。固定输入及预期在
[business/cases.json](business/cases.json) 和 [boundary/cases.json](boundary/cases.json)。
这些是用例定义，不代表执行成绩。通用复检方法见 [手册](../REVIEW.md)。

## 离线装置检查

```bash
backend/.venv/bin/python tests/run.py tests/e2e/test_harness.py -q
```

通过统一入口阻断网络和真实数据库，不发送聊天请求。

## 显式真实运行

先核对授权、模型费用、目标配置及脚本的写入范围。隔离运行会创建新的 PostgreSQL 数据库，
写合成账号、商品、订单、会话和审计；使用全新 `pc_e2e_` 名称，不复用共享数据库。
输出保存到忽略的 artifacts。运行结束不自动删除数据库，应另行安排私有备份与清理。

```bash
backend/.venv/bin/python -m tests.e2e.isolated artifacts/evals/e2e/new-batch \
  --database pc_e2e_new_fixture
```

入口可按 `--help` 选择用例分组；某些场景使用受控故障服务或本地政策资料，
实际调用边界以场景定义为准，不能将环境准备成功视为场景通过。

指定键盘推荐场景的复制目录入口：

```bash
backend/.venv/bin/python -m tests.e2e.catalog_repro \
  artifacts/evals/e2e/keyboard-new-batch \
  --database pc_e2e_keyboard_new --confirm-live-writes
```

该入口只读复制源商品目录到新库，创建合成账号，再执行真实聊天和追踪；
不复制源用户、订单或认证记录。仍需明确授权模型费用和新库写入。

批次冻结独立预期、源码、输入和证据。检查身份隔离、同 SKU 条件成立、回答/卡片一致性、
失败边界和 trace 暴露；程序检查与独立语义判读分别记录，保留未执行与无法判定。
代码或标注变化后新建批次，不覆盖旧证据。批次成绩、trace ID 和排错过程只写本地资料。
