"""Tests for the LXD test-node lifecycle operations.

Pattern: subprocess is replaced so no lxc, sudo or root access is needed.
What is asserted is the exact argv that WOULD have been run, the content of
the bash script templates for critical guards, and what the Python handler
returns or raises. Running the scripts for real is the e2e job on a node.

Three operations: lxc_node_provision, lxc_node_destroy, lxc_node_status.
"""
import json
import sys
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _ok_provision(name="sn-test-1", version="1.12.21"):
    return _Proc(out=json.dumps({
        "name": name, "version": version,
        "state": "running", "idempotent": False
    }))


def _ok_idempotent(name="sn-test-1"):
    return _Proc(out=json.dumps({
        "name": name, "state": "Running", "idempotent": True
    }))


def _ok_destroy(name="sn-test-1"):
    return _Proc(out=json.dumps({"name": name, "destroyed": True}))


def _ok_status(name="sn-test-1"):
    return _Proc(out=json.dumps({
        "name": name, "found": True, "state": "Running", "health": None
    }))


@pytest.fixture
def stub_run(monkeypatch):
    """Replace subprocess.run in operations; return a controller dict."""
    import operations
    ctrl = {}

    def _run(argv, **kw):
        ctrl["argv"] = argv
        rc = ctrl.get("rc", 0)
        out = ctrl.get("out", "")
        err = ctrl.get("err", "")
        return _Proc(rc=rc, out=out, err=err)

    monkeypatch.setattr(operations.subprocess, "run", _run)
    return ctrl


# ---------------------------------------------------------------------------
# lxc_node_provision -- argument validation (Python handler, before sudo)
# ---------------------------------------------------------------------------

class TestProvisionValidation:
    @pytest.mark.parametrize("name", [
        "", "A", "1abc", "HAS-UPPER", "has space", "has/slash",
        "a", "a" * 42, "$(whoami)", "semi;colon", "-leading",
    ])
    def test_bad_name_rejected(self, name):
        import operations
        with pytest.raises(ValueError, match="invalid container name"):
            operations._lxc_node_provision({"name": name, "installer_version": "1.0.0"})

    @pytest.mark.parametrize("ver", ["", " ", "\x00bad", "a" * 62])
    def test_bad_version_rejected(self, ver):
        import operations
        with pytest.raises(ValueError, match="invalid installer_version"):
            operations._lxc_node_provision({"name": "sn-test", "installer_version": ver})

    def test_seed_manifest_must_be_dict_or_null(self):
        import operations
        with pytest.raises(ValueError, match="seed_manifest must be"):
            operations._lxc_node_provision({
                "name": "sn-test", "installer_version": "1.0.0",
                "seed_manifest": "disconnected"
            })

    @pytest.mark.parametrize("key", ["lowercase", "1STARTS_DIGIT", "HAS SPACE", ""])
    def test_seed_manifest_key_validated(self, key):
        import operations
        with pytest.raises(ValueError, match="invalid seed_manifest key"):
            operations._lxc_node_provision({
                "name": "sn-test", "installer_version": "1.0.0",
                "seed_manifest": {key: "value"}
            })

    def test_seed_manifest_value_must_be_single_line(self):
        import operations
        with pytest.raises(ValueError, match="single line"):
            operations._lxc_node_provision({
                "name": "sn-test", "installer_version": "1.0.0",
                "seed_manifest": {"TOKEN": "line1\nline2"}
            })


# ---------------------------------------------------------------------------
# lxc_node_provision -- subprocess call (argv + result)
# ---------------------------------------------------------------------------

class TestProvisionCall:
    def test_argv_structure(self, stub_run, monkeypatch):
        import operations
        import config
        stub_run["out"] = json.dumps({
            "name": "sn-test-1", "version": "1.12.21",
            "state": "running", "idempotent": False
        })
        monkeypatch.setattr(config, "TEST_NODE_REGISTRY", "/var/lib/admin-runner/test-nodes.txt")
        monkeypatch.setattr(config, "LXC_BASE_IMAGE", "ubuntu:24.04")
        monkeypatch.setattr(config, "LXC_NETWORK", "lxdbr0")
        monkeypatch.setattr(config, "LXC_INSTALLER_URL_BASE", "")

        operations._lxc_node_provision({"name": "sn-test-1", "installer_version": "1.12.21"})

        argv = stub_run["argv"]
        assert argv[:4] == ["sudo", "-n", "bash", "-c"]
        assert argv[4] is operations._LXC_PROVISION_SH, "must pass the fixed template"
        assert argv[5] == "_"                          # $0 placeholder
        assert argv[6] == "sn-test-1"                 # NAME
        assert argv[7] == "1.12.21"                   # VERSION
        assert argv[8] == "/var/lib/admin-runner/test-nodes.txt"  # REGISTRY
        assert argv[9] == "ubuntu:24.04"              # BASE_IMAGE
        assert argv[10] == "lxdbr0"                   # NETWORK
        assert argv[11] == ""                          # INSTALLER_URL_BASE (empty)
        assert argv[12] == "disconnected"             # SEED_PATH (no manifest)

    def test_no_manifest_passes_disconnected(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({"name": "sn-test", "version": "1.0", "state": "running", "idempotent": False})
        operations._lxc_node_provision({"name": "sn-test", "installer_version": "1.0"})
        assert stub_run["argv"][-1] == "disconnected"

    def test_with_manifest_passes_temp_file_path(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({"name": "sn-test", "version": "1.0", "state": "running", "idempotent": False})
        operations._lxc_node_provision({
            "name": "sn-test", "installer_version": "1.0",
            "seed_manifest": {"TG_TOKEN": "test-token-abc"}
        })
        seed_arg = stub_run["argv"][-1]
        # Must NOT be "disconnected" when a manifest is provided
        assert seed_arg != "disconnected"
        # Must look like a file path
        assert seed_arg.startswith("/")

    def test_idempotent_result_returned(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({"name": "sn-test", "state": "Running", "idempotent": True})
        result = operations._lxc_node_provision({"name": "sn-test", "installer_version": "1.0"})
        assert result["idempotent"] is True

    def test_exit4_raises_valueerror(self, stub_run):
        import operations
        stub_run["rc"] = 4
        stub_run["err"] = "REFUSED: live hostname"
        with pytest.raises(ValueError, match="live node hostname"):
            operations._lxc_node_provision({"name": "sn-test", "installer_version": "1.0"})

    def test_exit5_raises_runtime_credential(self, stub_run):
        import operations
        stub_run["rc"] = 5
        stub_run["err"] = "REFUSED: credential matches"
        with pytest.raises(RuntimeError, match="live node credential"):
            operations._lxc_node_provision({"name": "sn-test", "installer_version": "1.0"})

    def test_other_nonzero_raises_runtime(self, stub_run):
        import operations
        stub_run["rc"] = 6
        stub_run["err"] = "container not ready"
        with pytest.raises(RuntimeError, match="lxc_node_provision failed"):
            operations._lxc_node_provision({"name": "sn-test", "installer_version": "1.0"})

    def test_not_dangerous(self):
        import operations
        op = operations.REGISTRY["lxc_node_provision"]
        assert op.is_dangerous({}) is False

    def test_registered(self):
        import operations
        op = operations.REGISTRY["lxc_node_provision"]
        assert op.handler is operations._lxc_node_provision
        assert "name" in op.input_schema["properties"]
        assert "installer_version" in op.input_schema["properties"]


# ---------------------------------------------------------------------------
# lxc_node_provision -- bash script text (critical guards must be present)
# ---------------------------------------------------------------------------

class TestProvisionScript:
    def test_hostname_guard_before_launch(self):
        """Guard 1 (hostname) must appear BEFORE the lxc launch line."""
        import operations
        sh = operations._LXC_PROVISION_SH
        hostname_pos = sh.find('hostname')
        launch_pos = sh.find('lxc launch')
        assert hostname_pos != -1, "hostname guard missing from script"
        assert launch_pos != -1, "lxc launch missing from script"
        assert hostname_pos < launch_pos, "hostname guard must precede lxc launch"

    def test_credential_check_before_launch(self):
        """Guard 3 (grep -qF) must appear BEFORE the actual lxc launch command."""
        import operations
        sh = operations._LXC_PROVISION_SH
        grep_pos = sh.find('grep -qF')
        # Search for the actual launch command, not just the substring "lxc launch"
        # which can appear in comments.
        launch_pos = sh.find('lxc launch "$BASE_IMAGE"')
        assert grep_pos != -1, "grep -qF credential check missing from script"
        assert launch_pos != -1, 'lxc launch "$BASE_IMAGE" missing from script'
        assert grep_pos < launch_pos, "credential check must precede lxc launch"

    def test_credential_check_reads_live_secrets_dir(self):
        """The check must read /etc/smart-node/*.env, not some other path."""
        import operations
        assert "/etc/smart-node/*.env" in operations._LXC_PROVISION_SH

    def test_exit_4_on_hostname_match(self):
        import operations
        assert "exit 4" in operations._LXC_PROVISION_SH

    def test_exit_5_on_credential_match(self):
        import operations
        assert "exit 5" in operations._LXC_PROVISION_SH

    def test_registry_append_after_provision(self):
        """Container name must be written to the registry file."""
        import operations
        sh = operations._LXC_PROVISION_SH
        assert 'echo "$NAME" >> "$REGISTRY"' in sh

    def test_registry_dir_created(self):
        import operations
        assert 'mkdir -p' in operations._LXC_PROVISION_SH

    def test_disconnected_mode_no_seed_file(self):
        """When SEED=disconnected the script must not attempt to read a file."""
        import operations
        sh = operations._LXC_PROVISION_SH
        assert '"disconnected"' in sh

    def test_installer_skipped_when_no_url(self):
        """Installer step is conditional on INSTALLER_URL_BASE being non-empty."""
        import operations
        assert '[ -n "$INSTALLER_URL_BASE" ]' in operations._LXC_PROVISION_SH

    def test_seed_flag_passed_to_installer(self):
        """Installer must receive --seed-manifest or --disconnected."""
        import operations
        sh = operations._LXC_PROVISION_SH
        assert "--seed-manifest" in sh
        assert "--disconnected" in sh


# ---------------------------------------------------------------------------
# lxc_node_destroy -- argument validation
# ---------------------------------------------------------------------------

class TestDestroyValidation:
    @pytest.mark.parametrize("name", ["", "BAD", "1start", "has space"])
    def test_bad_name_rejected(self, name):
        import operations
        with pytest.raises(ValueError, match="invalid container name"):
            operations._lxc_node_destroy({"name": name})


# ---------------------------------------------------------------------------
# lxc_node_destroy -- subprocess call
# ---------------------------------------------------------------------------

class TestDestroyCall:
    def test_argv_structure(self, stub_run, monkeypatch):
        import operations
        import config
        stub_run["out"] = json.dumps({"name": "sn-test-1", "destroyed": True})
        monkeypatch.setattr(config, "TEST_NODE_REGISTRY", "/var/lib/admin-runner/test-nodes.txt")

        operations._lxc_node_destroy({"name": "sn-test-1"})

        argv = stub_run["argv"]
        assert argv[:4] == ["sudo", "-n", "bash", "-c"]
        assert argv[4] is operations._LXC_DESTROY_SH
        assert argv[5] == "_"
        assert argv[6] == "sn-test-1"
        assert argv[7] == "/var/lib/admin-runner/test-nodes.txt"

    def test_exit4_raises_hostname_error(self, stub_run):
        import operations
        stub_run["rc"] = 4
        with pytest.raises(ValueError, match="live node hostname"):
            operations._lxc_node_destroy({"name": "sn-test"})

    def test_exit5_raises_not_in_registry(self, stub_run):
        import operations
        stub_run["rc"] = 5
        with pytest.raises(ValueError, match="not in the test-node registry"):
            operations._lxc_node_destroy({"name": "sn-test"})

    def test_nonzero_other_raises_runtime(self, stub_run):
        import operations
        stub_run["rc"] = 1
        stub_run["err"] = "lxc: error"
        with pytest.raises(RuntimeError, match="lxc_node_destroy failed"):
            operations._lxc_node_destroy({"name": "sn-test"})

    def test_is_dangerous(self):
        import operations
        op = operations.REGISTRY["lxc_node_destroy"]
        assert op.is_dangerous({}) is True

    def test_registered(self):
        import operations
        op = operations.REGISTRY["lxc_node_destroy"]
        assert op.handler is operations._lxc_node_destroy
        assert "name" in op.input_schema["properties"]


# ---------------------------------------------------------------------------
# lxc_node_destroy -- bash script text
# ---------------------------------------------------------------------------

class TestDestroyScript:
    def test_hostname_guard_before_delete(self):
        import operations
        sh = operations._LXC_DESTROY_SH
        hostname_pos = sh.find("hostname")
        delete_pos = sh.find("lxc delete")
        assert hostname_pos != -1 and delete_pos != -1
        assert hostname_pos < delete_pos

    def test_registry_guard_before_delete(self):
        """Registry check must run before any lxc command."""
        import operations
        sh = operations._LXC_DESTROY_SH
        registry_pos = sh.find('grep -qxF "$NAME" "$REGISTRY"')
        delete_pos = sh.find("lxc delete")
        assert registry_pos != -1, "registry guard missing"
        assert registry_pos < delete_pos

    def test_exit_4_on_hostname(self):
        import operations
        assert "exit 4" in operations._LXC_DESTROY_SH

    def test_exit_5_on_not_in_registry(self):
        import operations
        assert "exit 5" in operations._LXC_DESTROY_SH

    def test_registry_entry_removed_after_delete(self):
        """After deletion the name must be removed from the registry file."""
        import operations
        sh = operations._LXC_DESTROY_SH
        assert 'grep -vxF "$NAME"' in sh

    def test_force_flag_on_stop_and_delete(self):
        import operations
        sh = operations._LXC_DESTROY_SH
        assert "--force" in sh


# ---------------------------------------------------------------------------
# lxc_node_status
# ---------------------------------------------------------------------------

class TestStatusValidation:
    @pytest.mark.parametrize("name", ["", "BAD", "1start"])
    def test_bad_name_rejected(self, name):
        import operations
        with pytest.raises(ValueError, match="invalid container name"):
            operations._lxc_node_status({"name": name})


class TestStatusCall:
    def test_argv_structure(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({"name": "sn-test", "found": True, "state": "Running", "health": None})
        operations._lxc_node_status({"name": "sn-test"})
        argv = stub_run["argv"]
        assert argv[:4] == ["sudo", "-n", "bash", "-c"]
        assert argv[4] is operations._LXC_STATUS_SH
        assert argv[5] == "_"
        assert argv[6] == "sn-test"

    def test_not_found_returns_false(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({"name": "sn-test", "found": False})
        result = operations._lxc_node_status({"name": "sn-test"})
        assert result["found"] is False

    def test_found_returns_state(self, stub_run):
        import operations
        stub_run["out"] = json.dumps({
            "name": "sn-test", "found": True, "state": "Running", "health": None
        })
        result = operations._lxc_node_status({"name": "sn-test"})
        assert result["state"] == "Running"

    def test_nonzero_raises(self, stub_run):
        import operations
        stub_run["rc"] = 1
        stub_run["err"] = "lxc info: not found"
        with pytest.raises(RuntimeError, match="lxc_node_status failed"):
            operations._lxc_node_status({"name": "sn-test"})

    def test_not_dangerous(self):
        import operations
        op = operations.REGISTRY["lxc_node_status"]
        assert op.is_dangerous({}) is False

    def test_registered(self):
        import operations
        op = operations.REGISTRY["lxc_node_status"]
        assert op.handler is operations._lxc_node_status
        assert "name" in op.input_schema["properties"]


class TestStatusScript:
    def test_health_check_via_lxc_exec(self):
        """Health must be fetched inside the container, not from outside."""
        import operations
        sh = operations._LXC_STATUS_SH
        assert "lxc exec" in sh
        assert "/health" in sh

    def test_not_found_path(self):
        import operations
        assert '"found":false' in operations._LXC_STATUS_SH

    def test_state_extracted(self):
        import operations
        assert "Status:" in operations._LXC_STATUS_SH
