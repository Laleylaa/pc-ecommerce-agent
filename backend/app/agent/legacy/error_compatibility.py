"""Read new tool failures with legacy recovery policy; historical records stay readable.

Remove with the legacy recovery/answer consumers. This does not change the public
ToolError, stored evidence, or serialize old fields into new tool responses.
"""

from collections.abc import Mapping
from typing import Any


def legacy_error_view(error: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(error)
    if "next_action" not in error:
        return result
    action = error["next_action"]
    result["recommended_action"] = {
        "retry": "retry_once", "revise_arguments": "replan_arguments",
        "request_authentication": "request_authentication",
    }.get(action, "stop")
    result["retryable"] = action in {"retry", "revise_arguments"}
    # Existing legacy policy keys; not a second public error vocabulary.
    result["code"] = (
        "timeout" if action == "retry" else
        "invalid_input" if action == "revise_arguments" else error.get("code")
    )
    return result
