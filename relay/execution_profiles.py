"""Explicit execution-profile routing shared by local and cloud job paths."""

from __future__ import annotations

from enum import Enum


class ExecutionProfile(str, Enum):
    LOCAL_LOOP = "LOCAL_LOOP"
    CLOUD_WORKIQ = "CLOUD_WORKIQ"
    AUTO = "AUTO"


class RoutingError(ValueError):
    """Raised when a job cannot be routed without guessing."""


def resolve_profile(job: dict, capabilities: dict | None = None) -> ExecutionProfile:
    """Resolve an explicit/AUTO profile without silently changing runtimes."""
    capabilities = capabilities or {}
    try:
        requested = ExecutionProfile(str(job.get("execution_profile", "")))
    except ValueError as exc:
        raise RoutingError("job must contain a valid execution_profile") from exc

    if requested != ExecutionProfile.AUTO:
        return requested
    if bool(job.get("requires_local_tool")):
        return ExecutionProfile.LOCAL_LOOP

    location = str(job.get("data_location", "")).upper()
    if location in {"SHAREPOINT", "ONEDRIVE", "M365", "TEAMS", "OUTLOOK"}:
        return ExecutionProfile.CLOUD_WORKIQ
    if location == "LOCAL":
        return ExecutionProfile.LOCAL_LOOP
    raise RoutingError("execution profile cannot be resolved safely")


def validate_runtime(profile: ExecutionProfile, capabilities: dict) -> None:
    """Validate only the runtime required by the selected profile."""
    if profile == ExecutionProfile.LOCAL_LOOP:
        if not capabilities.get("local_mcp_available"):
            raise RoutingError("local MCP is required but unavailable")
        return
    if profile == ExecutionProfile.CLOUD_WORKIQ:
        if not capabilities.get("workiq_available"):
            raise RoutingError("WorkIQ/cloud tools are required but unavailable")
        return
    raise RoutingError("AUTO must be resolved before runtime validation")
