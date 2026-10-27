# 业务工具对外错误契约测试

范围：六个业务工具最终返回给主流程的错误契约、恢复动作和消费方接入。
内部规划/复核反馈不是本专项的公开错误；主流程准入与控制工具保留原边界。

本专项只覆盖代表路径，不穷举异常细码或重复完整商品业务矩阵：

| 文件 | 职责 |
| --- | --- |
| `cases.json` | 执行前确定的输入、分类/动作、调用次数及语义标准；额外合成输入与断言见pytest源码 |
| `test_error_contract.py` | 23条核心用例：12条错误出口、schema、六工具注册冒烟、业务结果及规划失败、重试/终止/预算、legacy局部兼容 |
| `conftest.py` | 保存实际输出、逐项比较与内存Trace；不发送Phoenix |
| `run_cases.py` | 冻结用例与源码，通过现有`tests/run.py`运行，输出XML、日志和`automatic.jsonl` |

从仓库根目录执行：

```bash
# 新建独立批次并保存证据；不覆盖旧结果
backend/.venv/bin/python tests/tools/error_contract/run_cases.py

# 只运行pytest，不生成专项报告批次
backend/.venv/bin/python tests/run.py tests/tools/error_contract -q
```

批次位于已忽略的`artifacts/evals/tool-error-contract/`。程序通过后，对照冻结标注、
实际记录与Trace逐例填写`checks/semantic.jsonl`和`report.md`；脚本不自动代签语义复检或人工接受。


遵循 [测试流程](../../README.md) 与 [复检手册](../../REVIEW.md)：先冻结独立预期，
再执行、程序检查、逐次语义复检；保留失败和无法判定记录，内部approve不作为通过依据。
离线检查必须经 `tests/run.py`；真实模型与数据库评测需单独授权。
此专项不替代真实模型、外部依赖或聊天端到端验收。
