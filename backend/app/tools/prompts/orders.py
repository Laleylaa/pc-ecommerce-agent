"""Order prompt template with executable capabilities and schema injected."""

import json

from pydantic import BaseModel

from app.tools.prompts.templates import render_prompt
from app.tools.structured_planning import output_schema


def build_order_prompt(supported_fields: frozenset[str], schema: type[BaseModel]) -> str:
    return render_prompt(
        "orders/plan.md",
        supported_fields=", ".join(sorted(supported_fields)),
        output_schema=json.dumps(
            output_schema(schema, schema.model_required_fields), ensure_ascii=False
        ),
    )
