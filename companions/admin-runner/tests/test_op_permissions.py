"""Per-op permission catalogue (admin-runner side).

The admin-runner has one authorisation chokepoint. Each entry in REGISTRY
declares the permission its caller must hold; that permission is what the
central permissions.require_permission() dependency looks up against
sn_config.role_permissions. If an entry drifts (missing permission, wrong
shape), the server must refuse to start rather than admit the handler by
accident. These tests are the structural guard against that drift.

The expected set of (registry key, permission) pairs comes from the signed
operations catalogue.
"""
from operations import REGISTRY, validate_registry


EXPECTED = {
    "node_status":              "admin.node_status",
    "run_command":              "admin.run_command",
    "service_control":          "admin.service_control",
    "materialize_crons":        "admin.materialize_crons",
    "render_agent_runtime_env": "admin.render_agent_runtime_env",
    "rollback_crons":           "admin.rollback_crons",
    "ensure_agent_users":       "admin.ensure_agent_users",
    "ensure_project":           "admin.ensure_project",
    "claude_login_start":       "admin.claude_login_start",
    "claude_login_complete":    "admin.claude_login_complete",
    "lxc_node_provision":       "admin.lxc_node_provision",
    "lxc_node_destroy":         "admin.lxc_node_destroy",
    "lxc_node_status":          "admin.lxc_node_status",
    "run_health_probe":         "admin.run_health_probe",
    "agent_memory":             "admin.agent_memory",
}


def test_registry_has_one_permission_per_op():
    missing = [k for k, op in REGISTRY.items() if not op.permission]
    assert not missing, (
        "every admin-runner op must declare a permission; "
        f"missing: {missing}"
    )


def test_registry_permission_names_match_catalogue():
    actual = {k: REGISTRY[k].permission for k in EXPECTED}
    assert actual == EXPECTED, (
        "admin-runner REGISTRY disagrees with the operations catalogue; "
        f"drift: expected {EXPECTED}, got {actual}"
    )


def test_registry_covers_all_ops_in_catalogue():
    # The catalogue lists 14 ops; the REGISTRY must list the same 14.
    assert set(REGISTRY) == set(EXPECTED), (
        "REGISTRY op set differs from the operations catalogue: "
        f"only in REGISTRY = {set(REGISTRY) - set(EXPECTED)}, "
        f"only in catalogue = {set(EXPECTED) - set(REGISTRY)}"
    )


def test_validate_registry_passes_on_the_shipped_registry():
    # The guard called from app.py lifespan must accept the shipped REGISTRY.
    validate_registry()


def test_validate_registry_rejects_a_row_with_no_permission(monkeypatch):
    from operations import Op
    bad = dict(REGISTRY)
    bad["bogus"] = Op("bogus", "no permission", lambda a: {}, dangerous=False)
    monkeypatch.setattr("operations.REGISTRY", bad)
    try:
        validate_registry()
    except RuntimeError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("validate_registry should reject a row with no permission")


def test_validate_registry_rejects_a_mismatched_permission_name(monkeypatch):
    from operations import Op
    bad = dict(REGISTRY)
    bad["node_status"] = Op("node_status", "x", lambda a: {},
                            permission="admin.something_else")
    monkeypatch.setattr("operations.REGISTRY", bad)
    try:
        validate_registry()
    except RuntimeError as exc:
        assert "node_status=admin.something_else" in str(exc)
    else:
        raise AssertionError(
            "validate_registry should reject a permission that is not admin.<key>"
        )
