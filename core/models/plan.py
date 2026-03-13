from enum import Enum

from sqlalchemy.dialects import postgresql


class Plan(str, Enum):
    FREE = "free"
    PERSONAL = "personal"
    PRO = "pro"
    STARTUP = "startup"


PLAN_TOKEN_LIMITS = {
    Plan.FREE: 0,
    Plan.PERSONAL: 500_000,
    Plan.PRO: 1_500_000,
    Plan.STARTUP: 3_000_000,
}


plan_enum = postgresql.ENUM(
    "free",
    "personal",
    "pro",
    "startup",
    name="plan",
    create_type=False,
)


def plan_from_identifier(identifier: str | None) -> Plan:
    if not identifier:
        return Plan.FREE

    normalized = identifier.lower()

    if "startup" in normalized:
        return Plan.STARTUP
    if "pro" in normalized:
        return Plan.PRO
    if "personal" in normalized:
        return Plan.PERSONAL

    return Plan.FREE


def token_limit_for_plan(plan_value: str | Plan | None) -> int:
    if plan_value is None:
        return PLAN_TOKEN_LIMITS[Plan.FREE]

    if isinstance(plan_value, Plan):
        return PLAN_TOKEN_LIMITS.get(plan_value, 0)

    try:
        plan_enum_value = Plan(plan_value)
    except ValueError:
        plan_enum_value = Plan.FREE

    return PLAN_TOKEN_LIMITS.get(plan_enum_value, 0)
