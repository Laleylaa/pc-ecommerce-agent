"""Pure output-budget definitions shared by contracts, schemas and control tools."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_OUTPUT_TOKEN_BUDGET = 12000
MAX_OUTPUT_TOKEN_BUDGET = 16000
MAX_RESULT_ITEMS = 10


def output_cost(value: Any) -> int:
    """Conservative UTF-8 byte token upper bound; not provider usage accounting.

    No network/tokenizer download. Counting each byte as one token deliberately
    overestimates byte-based tokenization, including Chinese and JSON overhead.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class OutputBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_limit: int = Field(default=DEFAULT_OUTPUT_TOKEN_BUDGET, ge=1,
                             le=MAX_OUTPUT_TOKEN_BUDGET)
    counting_method: str = "utf8_byte_upper_bound"
    truncated: bool = False
    omitted_sections: list[str] = Field(default_factory=list)
    requested_item_count: int | None = None
    original_item_count: int | None = None
    returned_item_count: int | None = None
    message: str = ""


class BoundedToolOutput(BaseModel):
    output_budget: OutputBudget | None = None


def budget_description(token_limit: int, item_limit: int | None) -> str:
    count = (f" limit must be between 1 and {item_limit}; larger values are invalid."
             if item_limit else "")
    return (
        f" Output budget: {token_limit} conservative token units (UTF-8 byte upper bound)."
        + count
        + " Preserve the user's full request in query, but request only a legal display window."
        " Output may contain fewer entries due to size; read output_budget and completeness."
        " Explain partial delivery and invite a narrower request; never claim omitted facts"
        " are absent. Do not repeat calls automatically to bypass these limits."
    )
