# Demo 数据

正式入口为根目录 `make demo-info`（只读）和 `make db-init`（迁移后写入空库）。
本目录只保存受审查的 manifest 和说明；实际导出、备份继续存放在忽略的 artifacts/backups。

| 来源 | 内容 |
| --- | --- |
| `backend/scripts/demo/catalog.py` | 确定性生成 6 类、192 个 SPU、2,304 个 SKU |
| `accounts.py` | 6 个公开演示账号，密码为 `demo-password` |
| `orders.py` | 26 笔合成订单、明细与物流，不携带测试标记 |
| `backend/data/knowledge_documents.json` | 唯一知识正文来源，检索与数据库导入共用 |
| `backend/data/knowledge_vector_index.json` | 配套索引，保留文档哈希及模型标识 |

账号为 `demo@example.com`、`demo2@example.com` 至 `demo6@example.com`。
第一个账号有一笔已发货订单，其他账号各有待付款、待发货、已发货、已完成、已关闭五笔订单。
所有订单金额与所选 SKU 一致，时间基于初始化时同一个 UTC 基准，物流发生在付款和发货之后。
订单号只是固定演示标识，不代表真实日期。账号无真实手机号，收货信息为显式演示占位资料。

没有预置聊天、偏好、身份令牌或 trace；用户体验时自行生成。
商品名称中品牌仅用于演示识别，型号、价格、库存和规格是合成数据，不代表品牌真实产品。
政策正文为本项目 Demo 材料，不代表任何真实店铺承诺。团队项目来源见根 README。

导入前检查全部应用表，非空则拒绝；不覆盖已有安装，不重置密码。
整个批次在一个事务内写入，两个初始化器通过 PostgreSQL 事务锁串行化。
初始化期间不要启动后端或其他写入程序。再次启动已有环境请跳过 db-init。

旧 `seed_user_isolation.py` 继续用于开发验证，与公开 Demo 独立；默认 setup 不调用它。
本地数据库快照不作为公开数据来源。未来需要冻结数据包时，从此版本的输入在新空库生成，
标记源码提交、迁移版本及校验值后再发布，不能直接发布开发数据库。
