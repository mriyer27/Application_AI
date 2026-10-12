"""Deterministic permission checks. Prompts never grant or revoke permissions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from job_agent.errors import PermissionDenied
from job_agent.models import Permission

if TYPE_CHECKING:  # pragma: no cover
    from job_agent.config import AppConfig

#: Permissions whose actions always additionally require a consumed human approval.
APPROVAL_REQUIRED: frozenset[Permission] = frozenset(
    {Permission.SUBMIT_APPLICATIONS, Permission.SEND_EMAILS, Permission.CHANGE_CONFIG}
)

#: Permissions that are not implemented in this version, regardless of config.
NOT_IMPLEMENTED: frozenset[Permission] = frozenset({Permission.SEND_EMAILS})


def is_allowed(config: "AppConfig", permission: Permission) -> bool:
    if permission in NOT_IMPLEMENTED:
        return False
    return config.permissions.allows(permission)


def require_permission(config: "AppConfig", permission: Permission) -> None:
    if permission in NOT_IMPLEMENTED:
        raise PermissionDenied(
            f"'{permission.value}' is not implemented in this version; nothing was sent.",
            human_action="Send the message yourself from your mail client.",
        )
    if not config.permissions.allows(permission):
        raise PermissionDenied(
            f"permission '{permission.value}' is disabled in config (permissions.{permission.value}).",
            human_action=f"Enable permissions.{permission.value} in config/config.yaml if intended.",
        )
