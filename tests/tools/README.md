# 工具测试

按公开工具名选择目录：`catalog_search/`、`catalog_lookup/`、`catalog_facets/`、
`order_lookup/`、`policy_search/`、`knowledge_search/`；共享装置与规则在 `shared/`。
`error_contract/` 覆盖业务工具错误交付；`shared/local_retrieval/` 覆盖本地检索服务。
`catalog/coverage.json` 只保存商品测试场景分布，运行结果不在其中记录。

```bash
backend/.venv/bin/python tests/run.py tests/tools -q
backend/.venv/bin/python tests/run.py tests/tools/catalog_lookup -q
```

以上入口使用离线保护。真实商品评测由 `shared/run_catalog_cases.py` 显式运行，
使用前阅读 [商品评测说明](shared/CATALOG_REVIEW.md)，确认模型费用及数据库只读范围。
`prepare` 冻结输入、预期与版本，`run` 使用指定批次；不要把准备成功视为业务通过。

```bash
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --prepare
backend/.venv/bin/python tests/tools/shared/run_catalog_cases.py lookup --live --run <批次目录名>
backend/.venv/bin/python tests/tools/shared/check_catalog_cases.py --render <批次目录名>
```

search/facets 使用同一入口相应参数。实际结果在忽略的 `catalog/results/`，
开发计划与失败调查只写本地 docs，不回填到公开说明或 coverage。
