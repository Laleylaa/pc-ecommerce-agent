# 第三方组件与许可

本项目的 MIT 许可证适用于本项目代码，不替换第三方框架、模型或服务的许可。
项目源自团队共同开发的 [Loda951/pc-agnet](https://github.com/Loda951/pc-agnet)，
保留原团队贡献说明；此仓库的许可声明不改变原仓库自身的授权状态。

主要直接依赖的许可如下。具体版本见 `backend/uv.lock` 和 `frontend/package-lock.json`，
完整条款及版权声明以相应版本随包提供的 LICENSE / NOTICE 为准。

| 组件 | 许可 |
| --- | --- |
| FastAPI、Pydantic、pydantic-settings、SQLAlchemy、Alembic | MIT |
| langchain-core、langchain-openai | MIT |
| Strands Agents、Sentence Transformers、asyncpg | Apache-2.0 |
| HTTPX、Uvicorn、python-dotenv | BSD-3-Clause |
| React、React DOM、react-markdown、remark-gfm | MIT |
| Vite、React Vite 插件、Node/React 类型定义 | MIT |
| TypeScript | Apache-2.0 |
| Lucide React | ISC |
| [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) 政策检索模型 | MIT |
| [PostgreSQL](https://www.postgresql.org/about/licence/) | PostgreSQL License |

本表不穷举传递依赖，也不替代许可证原文。例如 certifi 使用 MPL-2.0，orjson、tqdm
的许可声明也包含 MPL-2.0；这些第三方部分仍须遵循各自条款，不能统一改标为 MIT。
分发包含第三方组件的前端构建包、镜像或安装包时，应保留其要求的版权、许可证和 NOTICE，
并履行适用的源码提供义务；仅添加本文件不能替代这些义务。

依赖、模型权重和 PostgreSQL 镜像通过安装步骤取得，不直接纳入本仓库源码。
模型与搜索 API 还受提供方的服务条款约束，项目许可证不授予 API 访问权限。
