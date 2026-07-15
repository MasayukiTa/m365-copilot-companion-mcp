import pytest

from relay.execution_profiles import (
    ExecutionProfile,
    RoutingError,
    resolve_profile,
    validate_runtime,
)


def test_explicit_profiles_are_never_rewritten():
    assert resolve_profile({"execution_profile": "LOCAL_LOOP"}) == ExecutionProfile.LOCAL_LOOP
    assert resolve_profile({"execution_profile": "CLOUD_WORKIQ"}) == ExecutionProfile.CLOUD_WORKIQ


def test_auto_routes_by_required_runtime_and_location():
    assert resolve_profile({
        "execution_profile": "AUTO", "requires_local_tool": True,
        "data_location": "SHAREPOINT",
    }) == ExecutionProfile.LOCAL_LOOP
    assert resolve_profile({
        "execution_profile": "AUTO", "requires_local_tool": False,
        "data_location": "ONEDRIVE",
    }) == ExecutionProfile.CLOUD_WORKIQ
    assert resolve_profile({
        "execution_profile": "AUTO", "data_location": "LOCAL",
    }) == ExecutionProfile.LOCAL_LOOP


def test_auto_refuses_to_guess():
    with pytest.raises(RoutingError):
        resolve_profile({"execution_profile": "AUTO", "data_location": "UNKNOWN"})


def test_cloud_runtime_never_checks_local_mcp():
    validate_runtime(ExecutionProfile.CLOUD_WORKIQ, {
        "workiq_available": True,
        "local_mcp_available": False,
    })
    with pytest.raises(RoutingError, match="WorkIQ"):
        validate_runtime(ExecutionProfile.CLOUD_WORKIQ, {
            "workiq_available": False,
            "local_mcp_available": True,
        })
