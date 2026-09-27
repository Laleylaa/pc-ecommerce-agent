# 仓库指南

## 项目

PC 外设商城客服 Agent；Python/FastAPI/LangGraph 后端，React/TypeScript/Vite 前端。运行教程以根目录 README 为唯一入口，当前模块技术说明见 docs/，不维护阶段性协作流水账。

## 统一命令

- 环境检查：`make doctor`；安装依赖：`make install`。
- 首次初始化：`make setup-local`，会写目标数据库；复用已有数据按 README 的独立流程操作。
- 日常运行：`make infra-up`，两个终端分别运行 `make backend`、`make frontend`。
- 检查：`make check`；后端单独 `make backend-test`，前端单独 `make frontend-build`。
- 后端用 uv、Python 3.11 和 `backend/uv.lock`；前端安装用 `npm ci`，推荐 Node 24，最低 Node 22。

## 代码职责

- router 保持薄层，业务逻辑放 services，数据库查询放 repositories，模型放 models。
- 编排位于 backend/app/agent，正式工具契约位于 backend/app/tools。
- backend/data 的知识 JSON 和向量索引是运行输入，不是可直接删除的缓存。
- Python 4 空格缩进、类型标注、Ruff 行宽 100；React 组件 PascalCase。

## 约束

- 不提交真实 .env、API key、数据库备份或用户数据；只提交脱敏配置模板。
- 已合入 Alembic migration 不修改，结构变化新增迁移。
- 单元测试隔离数据库与真实 LLM；数据库集成测试明确目标，不把 skipped 算通过。
- 本地容器统一使用 Podman 和现有脚本，不新增 Docker 专属命令。
- 修改端口、CORS、环境变量名称时同步更新模板、README 及对应模块文档。
- 依赖变更同步维护锁文件；普通安装不自动更新锁。
- 后端从源码定位根目录 .env；前端配置只放 frontend/.env.local，不能将服务端秘密放入 VITE_*。
- 默认 Podman 资源仍为 pc-agent；与其他项目共用配置时会共用数据库，不能随意 reset。
- 保留团队项目来源说明；提交使用 feat:、fix:、docs: 等简短前缀。
