from app.agent.boundary import BOUNDARY_POLICY
from app.agent.legacy.prompts import REQUEST_ROUTER_SYSTEM_PROMPT, SECURITY_AND_PRIVACY_POLICY


def test_router_and_security_prompts_use_authoritative_boundary_policy() -> None:
    assert BOUNDARY_POLICY.router_prompt in REQUEST_ROUTER_SYSTEM_PROMPT
    assert BOUNDARY_POLICY.security_prompt == SECURITY_AND_PRIVACY_POLICY
    assert BOUNDARY_POLICY.security_prompt in REQUEST_ROUTER_SYSTEM_PROMPT
