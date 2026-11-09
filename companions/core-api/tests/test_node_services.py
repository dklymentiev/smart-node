"""Host service collector: pure parsing/health mapping + the endpoint's
freshness gate (fresh sweep -> live counts; stale/absent -> honest null).

The pure tests need no DB. The endpoint tests seed sn_core.node_services +
node_service_sweeps directly and clean up after themselves.
"""

import pytest

from lib.node_services import parse_docker_ps, parse_systemctl


# --------------------------------------------------------------- docker parsing

def test_parse_docker_ps_states_map_to_health():
    out = "\n".join([
        '{"Names":"smart-node-db","Image":"pgvector/pgvector:pg16","State":"running","Status":"Up 2 hours"}',
        '{"Names":"smart-node-backend","Image":"core:latest","State":"exited","Status":"Exited (1) 5m ago"}',
        '{"Names":"smart-node-x","Image":"x","State":"restarting","Status":"Restarting"}',
    ])
    rows = parse_docker_ps(out)
    by = {r["name"]: r for r in rows}
    assert by["smart-node-db"]["health"] == "ok"
    assert by["smart-node-db"]["version"] == "pgvector/pgvector:pg16"
    assert by["smart-node-backend"]["health"] == "fail"
    assert by["smart-node-x"]["health"] == "warn"


def test_parse_docker_ps_skips_blank_and_garbage_lines():
    out = '\n\nnot json\n{"Names":"ok","State":"running"}\n'
    rows = parse_docker_ps(out)
    assert len(rows) == 1 and rows[0]["name"] == "ok"


def test_parse_docker_ps_unknown_state_is_warn():
    rows = parse_docker_ps('{"Names":"weird","State":"transmogrifying"}')
    assert rows[0]["health"] == "warn"


# -------------------------------------------------------------- systemd parsing

def test_parse_systemctl_active_running_is_ok():
    out = "smart-node-core.service loaded active running Smart Node core API"
    r = parse_systemctl(out)[0]
    assert r["health"] == "ok"
    assert r["status"] == "active (running)"
    assert r["detail"] == "Smart Node core API"


def test_parse_systemctl_failed_is_fail_and_inactive_is_warn():
    out = "\n".join([
        "a.service loaded failed failed Some unit",
        "b.service loaded inactive dead Another unit",
    ])
    by = {r["name"]: r for r in parse_systemctl(out)}
    assert by["a.service"]["health"] == "fail"
    assert by["b.service"]["health"] == "warn"


def test_parse_systemctl_skips_short_lines():
    assert parse_systemctl("\nshort line\n") == []


# ------------------------------------------------- endpoint: the collector file
#
# /api/system/services and /api/system/crons serve what the host collector
# (host-tools/sn-panel-data.sh) last wrote; the panel never reads those files
# itself. The retired sn_core.node_services sweep is no longer read.
# These call the route functions directly with the collector directory pointed
# at a temp dir, so they need no database and no host.

import json
import os
import time

from fastapi import HTTPException

from routes import system


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "HOST_DATA_DIR", str(tmp_path))

    def _write(name, data, age_secs=0):
        p = tmp_path / name
        p.write_text(json.dumps(data))
        if age_secs:
            t = time.time() - age_secs
            os.utime(p, (t, t))
    return _write


SERVICES = {
    "companions": [{"kind": "companion", "name": "memory", "version": "0.3.3",
                    "state": "installed", "health": "ok", "path": "/opt/smart-node/memory"}],
    "containers": [{"kind": "container", "name": "smart-node-doci", "health": "ok"},
                   {"kind": "container", "name": "smart-node-authum", "health": "fail"}],
    "systemd": [{"kind": "systemd", "name": "smart-node-retention.service", "health": "ok",
                 "status": "idle, last run success (smart-node-retention.timer)",
                 "timer": "smart-node-retention.timer", "last_result": "success"}],
    "summary": {"companions_total": 1, "containers_running": 1, "containers_total": 2,
                "systemd_active": 1, "systemd_total": 1, "issues": 1},
    "generated_at": "2026-09-23T03:00:00Z",
}


def test_services_serves_the_collector_file(collector):
    collector("services.json", SERVICES)
    b = system.services(principal=None)
    assert b["summary"]["containers_total"] == 2
    assert b["summary"]["issues"] == 1
    assert {c["name"] for c in b["containers"]} == {"smart-node-doci", "smart-node-authum"}
    assert b["collector"]["stale"] is False


def test_services_old_file_is_flagged_stale(collector):
    collector("services.json", SERVICES, age_secs=10_000)
    b = system.services(principal=None)
    assert b["collector"]["stale"] is True          # still returned, but marked
    assert b["summary"]["containers_total"] == 2


def test_services_without_collector_is_503(collector):
    with pytest.raises(HTTPException) as e:
        system.services(principal=None)
    assert e.value.status_code == 503


def test_crons_serves_the_collector_file(collector):
    collector("crons.json", {"timers": [{"name": "sn-panel-data.timer", "scope": "smart-node"}],
                             "crontabs": [], "summary": {"timers_total": 1, "crontab_entries": 0}})
    b = system.crons(principal=None)
    assert b["summary"]["timers_total"] == 1
    assert b["timers"][0]["name"] == "sn-panel-data.timer"
