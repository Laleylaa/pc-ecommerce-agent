"""Tool-selection examples for LLM2; public tool schemas remain in tools/contracts.py."""


def _example(query: str, tool: str, **arguments) -> dict:
    return {
        "input": {
            "goals": [
                {
                    "id": "goal_1",
                    "query": query,
                    "source_text": query,
                    "context_refs": [],
                }
            ]
        },
        "output": {
            "tasks": [
                {
                    "id": "task_1",
                    "goal_id": "goal_1",
                    "tool_name": tool,
                    "arguments": {"query": query, **arguments},
                    "depends_on": [],
                    "answer_role": "user_facing",
                }
            ],
            "blocked_goals": [],
        },
    }


TASK_PLANNING_EXAMPLES = [
    _example("找白色蓝牙键盘，300元以内", "catalog_search", category_hint="键盘"),
    _example("键盘有哪些布局可以选？", "catalog_facets"),
    {
        "input": {"goals": [{
            "id": "goal_1", "query": "比较Keychron K8和罗技K380的价格与连接方式",
            "source_text": "比较Keychron K8和罗技K380的价格与连接方式", "context_refs": [],
        }]},
        "output": {
            "tasks": [
                {"id": "task_1", "goal_id": "goal_1", "tool_name": "catalog_search",
                 "arguments": {"query": "查询Keychron K8的价格与连接方式"},
                 "depends_on": [], "answer_role": "user_facing"},
                {"id": "task_2", "goal_id": "goal_1", "tool_name": "catalog_search",
                 "arguments": {"query": "查询罗技K380的价格与连接方式"},
                 "depends_on": [], "answer_role": "user_facing"},
            ],
            "blocked_goals": [],
        },
    },
    _example("查我的最近订单和物流", "order_lookup"),
    _example("店里键盘拆封后还能七天无理由退货吗？", "policy_search"),
    _example("检索机械键盘轴体工作原理的知识资料", "knowledge_search"),
    {
        "input": {
            "goals": [
                {
                    "id": "goal_1",
                    "query": "办公室用，白色蓝牙键盘300元以内，看看可选布局",
                    "source_text": "办公室用，白色蓝牙键盘300元以内，看看可选布局",
                    "context_refs": [],
                }
            ]
        },
        "output": {
            "tasks": [
                {
                    "id": "task_1",
                    "goal_id": "goal_1",
                    "tool_name": "catalog_search",
                    "arguments": {
                        "query": "办公室用，白色蓝牙键盘300元以内",
                        "category_hint": "键盘",
                    },
                    "depends_on": [],
                    "answer_role": "user_facing",
                },
                {
                    "id": "task_2",
                    "goal_id": "goal_1",
                    "tool_name": "catalog_facets",
                    "arguments": {"query": "白色蓝牙键盘300元以内有哪些布局可选？"},
                    "depends_on": [],
                    "answer_role": "user_facing",
                },
            ],
            "blocked_goals": [],
        },
    },
    _example("推荐两款300元以内的键盘", "catalog_search", limit=2),
    _example("列出最近两笔订单", "order_lookup", limit=2),
    _example("查订单123的物流", "order_lookup", order_id=123),
    _example("检索四段关于机械键盘轴体的资料", "knowledge_search", limit=4),
    _example("检索店铺退货政策，最后分三点说明", "policy_search"),
]
