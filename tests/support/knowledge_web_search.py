import httpx
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    execute_bound_tool,
)
from app.tools.web_search import WebSearchToolService

HIT = {
    "title": "磁轴技术说明", "url": "https://example.com/switches",
    "snippet": "磁轴技术简介", "summary": "磁轴通过霍尔传感器检测位移。",
    "content": "", "rerank_score": 2.4,
}


async def execute(handler, args=None):
    contract = DefaultToolContractProvider().get_contract("knowledge_search")
    return await execute_bound_tool(
        BoundTool(contract, handler), args or {"query": "磁轴和机械轴有什么区别？"}, {"user_id": 7},
    )


def service(handler, key="test-secret"):
    return WebSearchToolService(key, transport=httpx.MockTransport(handler))
