"""Wire regressions on an installed disposable node; no product code is imported.

Run as root with SN_E2E_DISPOSABLE=1. The creation scenario uses the real runner,
gateway and Claude CLI. A configured CLI account is a prerequisite, not a skip.
Before updating, capture the existing configurator with --capture-john FILE (on whatever release the
stand runs); pass that same file as E2E_ONBOARDING_BASELINE after the update.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.error

REPO = Path(__file__).resolve().parents[2]
SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
DB = os.environ.get("SMART_NODE_PG_CORE_DB", "smartnode")
RUN = uuid.uuid4().hex[:10]
OPERATOR_API = None
OPERATOR = None


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True,
                          timeout=kwargs.pop("timeout", 130), **kwargs).stdout.strip()


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    return run("runuser", "-u", "postgres", "--", "psql", "-X", "-v",
               "ON_ERROR_STOP=1", "-d", DB, "-At", "-c", statement)


def rows(statement):
    return json.loads(sql("SELECT coalesce(json_agg(r), '[]'::json) FROM (" +
                          statement + ") r"))


def api(path, method="GET", body=None, user=None):
    global OPERATOR_API, OPERATOR
    if not user:
        # 1.12.47 admits the local operator over trusted loopback; root on the
        # agent socket is deliberately unknown-peer on that baseline. Resolve
        # once before creation, so no lookup separates create and first message.
        if OPERATOR_API is None:
            OPERATOR_API = rows("SELECT endpoint FROM sn_config.integrations "
                                "WHERE slug='core-api'")[0]["endpoint"].rstrip("/")
            OPERATOR = rows("SELECT email FROM sn_config.users WHERE id=1")[0]["email"]
        headers = {"Remote-User": OPERATOR}
        proof_file = Path("/etc/smart-node/core-api-proxy-auth.env")
        if proof_file.is_file():
            for line in proof_file.read_text().splitlines():
                if line.startswith("SN_PROXY_AUTH_SECRET="):
                    proof = line.partition("=")[2].strip()
                    if proof:
                        headers["X-SN-Proxy-Auth"] = proof
                    break
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            OPERATOR_API + "/api/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=125) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            payload = error.read()
            return error.code, json.loads(payload) if payload else None
    cmd = ["runuser", "-u", user, "--", "curl", "-sS", "--max-time", "125",
           "-X", method, "-w", "\n%{http_code}", "--unix-socket", SOCKET,
           "http://localhost/api/" + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    output = run(*cmd, input=json.dumps(body) if body is not None else None)
    text, status = output.rsplit("\n", 1) if "\n" in output else ("", output)
    return int(status), json.loads(text) if text else None


def require(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS:", message, flush=True)


@contextlib.contextmanager
def services(**wanted):
    previous = {name: subprocess.run(["systemctl", "is-active", "--quiet", name],
                                    check=False).returncode == 0 for name in wanted}
    try:
        for name, active in wanted.items():
            run("systemctl", "start" if active else "stop", name)
            if name == "smart-node-admin-runner" and active:
                endpoint = rows("SELECT endpoint FROM sn_config.integrations "
                                "WHERE slug='admin-runner'")[0]["endpoint"].rstrip("/")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    result = subprocess.run(["curl", "-fsS", "--max-time", "2",
                                             endpoint + "/healthz"],
                                            capture_output=True, check=False)
                    if result.returncode == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("admin-runner HTTP service did not become ready")
        yield
    finally:
        for name, active in previous.items():
            run("systemctl", "start" if active else "stop", name)


def service_state(runner=False, admin=True):
    return services(**{"smart-node-runner": runner, "smart-node-admin-runner": admin})


@contextlib.contextmanager
def fixture_agents():
    slugs = []
    messages = []
    try:
        yield slugs, messages
    finally:
        for slug in slugs:
            status, _ = api("agents/" + slug, "PUT", {"enabled": False})
            if status == 200:
                api("agents/" + slug, "DELETE")
        if messages:
            ids = ",".join(str(int(mid)) for mid in messages)
            # Only this run's fixture messages; production append-only guards stay on.
            sql("BEGIN; SET LOCAL session_replication_role=replica; "
                "DELETE FROM sn_core.message_state WHERE message_id IN (" + ids + "); "
                "DELETE FROM sn_core.messages WHERE id IN (" + ids + "); COMMIT")


def agent_body(slug, title=None):
    return {"slug": slug, "title": title or "E2E creation probe",
            "model": os.environ.get("E2E_CREATE_MODEL", "claude-cli/sonnet"),
            "specialist_body": "Reply with E2E-READY. Do not use tools or contact anyone."}


def create():
    slug = "e2e-create-" + RUN
    since = str(int(time.time()))
    with service_state(runner=True), fixture_agents() as (slugs, messages):
        slugs.append(slug)
        status, agent = api("agents", "POST", agent_body(slug))
        created_at = time.monotonic()
        # No lookup, logging, grant changes or user creation between these requests.
        sent_at = time.monotonic()
        send_status, sent = api("messages/inbound", "POST", {
            "recipient": slug, "sender": "system", "channel_type": "internal",
            "content": "E2E immediate first task: reply with E2E-READY only."})
        delta = sent_at - created_at
        print(f"  create HTTP={status}; message HTTP={send_status}; delay={delta:.6f}s")
        require(status == 201, "creation returns 201")
        require(send_status == 200 and sent.get("id"), "internal message is committed")
        messages.append(sent["id"])
        require(delta < 1, "message request starts within one second of creation response")
        require(agent["model"].startswith("claude-cli/"), "probe uses the OS-user CLI path")
        cli = Path(rows("SELECT binary_path FROM sn_config.cli_providers "
                        "WHERE slug='claude-cli'")[0]["binary_path"])
        cli_paths = {str(cli), str(cli.resolve())}
        try:
            uid = pwd.getpwnam(agent["os_user"]).pw_uid
        except KeyError:
            uid = None
        started = False
        deadline = time.monotonic() + float(os.environ.get("E2E_START_TIMEOUT", "60"))
        while time.monotonic() < deadline:
            if uid is not None:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        if entry.stat().st_uid != uid:
                            continue
                        args = (entry / "cmdline").read_bytes().split(b"\0")[:2]
                        cwd = str((entry / "cwd").resolve())
                        executable = str((entry / "exe").resolve())
                        if (executable in cli_paths or
                            any(os.fsdecode(arg) in cli_paths for arg in args)) and cwd == "/srv/agents/" + slug:
                            started = True
                            break
                    except (OSError, PermissionError):
                        continue
            if started:
                break
            time.sleep(0.2)
        log = run("journalctl", "-u", "smart-node-gateway", "-u", "smart-node-runner",
                  "--since", "@" + since, "--no-pager", "-o", "cat")
        unknown = [line for line in log.splitlines()
                   if "unknown user" in line.lower() and agent["os_user"] in line]
        print(f"  CLI process under agent uid observed={started}; unknown-user errors={len(unknown)}")
        require(started, "real CLI process starts as the new agent from its own directory")
        require(not unknown, "gateway and runner report no unknown-user error for this agent")


def rollback():
    slug = "e2e-rollback-" + RUN
    with service_state(admin=False), fixture_agents() as (slugs, _):
        slugs.append(slug)
        status, _ = api("agents", "POST", agent_body(slug))
        state = rows("SELECT a.id, a.archived_at FROM sn_config.agents a WHERE a.slug=" + quote(slug))
        identities = rows("SELECT id FROM sn_config.users WHERE handle=" + quote(slug) +
                          " OR email=" + quote(slug))
        specialists = rows("SELECT id FROM sn_config.specialists WHERE slug=" + quote(slug))
        bindings = rows("SELECT ac.agent_id FROM sn_config.agent_channels ac JOIN "
                        "sn_config.agents a ON a.id=ac.agent_id WHERE a.slug=" + quote(slug))
        directory = Path("/srv/agents", slug).exists()
        print(f"  HTTP={status}; agents={len(state)}; identities={len(identities)}; "
              f"specialists={len(specialists)}; bindings={len(bindings)}; directory={directory}")
        failures = []
        for condition, message in [
                (500 <= status < 600, "stopped admin-runner makes creation fail"),
                (not state, "no agent row, including archived rows, remains"),
                (not identities and not specialists, "no agent identity or inline specialist remains"),
                (not bindings, "no channel binding remains"),
                (not directory, "no agent directory remains")]:
            try:
                require(condition, message)
            except AssertionError as error:
                print("  FAIL:", error)
                failures.append(str(error))
        require(not failures, "creation rollback is complete")


def ensure_fixture_users(agents):
    # Old releases defer this to cron. Provision mailbox-test identities through
    # the real privileged API so its red result measures routing, not user creation.
    endpoint = rows("SELECT endpoint FROM sn_config.integrations WHERE slug='admin-runner' "
                    "AND archived_at IS NULL")[0]["endpoint"]
    owner = rows("SELECT coalesce(nullif(handle,''),email) AS identity "
                 "FROM sn_config.users WHERE id=1")[0]["identity"]
    headers = {"Remote-User": owner, "Content-Type": "application/json"}
    runner_env = Path("/etc/smart-node/admin-runner.env")
    if runner_env.is_file():
        for line in runner_env.read_text().splitlines():
            if line.startswith("SN_ADMIN_CLIENT_SECRET="):
                secret = line.partition("=")[2].strip()
                if secret:
                    headers.update({"X-SN-Service": "core-api",
                                    "X-SN-Service-Auth": secret,
                                    "X-SN-On-Behalf-Of": owner})
                break
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/operations/ensure_agent_users",
        data=json.dumps({"args": {"agents": [
            {"slug": a["slug"], "os_user": a["os_user"]} for a in agents
        ]}}).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    require(result.get("ok") is True and result["result"]["failed"] == 0,
            "mailbox fixture identities provisioned by admin-runner")


def mailbox():
    box = "e2e-mail-" + RUN + "@example.invalid"
    with service_state(), fixture_agents() as (slugs, messages):
        agents = []
        for prefix, title in [("hazel", "Hazel"), ("peer", "E2E second receiver")]:
            slug = "e2e-" + prefix + "-" + RUN
            slugs.append(slug)
            status, agent = api("agents", "POST", agent_body(slug, title))
            require(status == 201, "mailbox fixture agent created: " + title)
            agents.append(agent)
        ensure_fixture_users(agents)
        hazel, peer = agents
        binding = {"enabled": True, "instance_ref": box}
        status, _ = api("agents/" + hazel["slug"] + "/channels/email", "PUT", binding)
        require(status == 200, "Hazel holds the mailbox")
        status, refused = api("agents/" + peer["slug"] + "/channels/email", "PUT", binding)
        print(f"  second binding HTTP={status}; response={json.dumps(refused)}")
        failures = []

        def check(condition, message):
            try:
                require(condition, message)
            except AssertionError as error:
                print("  FAIL:", error)
                failures.append(str(error))

        check(status == 409, "second binding is refused with 409")
        check(status == 409 and "Hazel" in str(refused), "refusal names Hazel")
        # Always build the same direct-DB duplicate, even when the old API accepted it.
        sql("DELETE FROM sn_config.agent_channels WHERE agent_id=" + str(peer["id"]) +
            " AND channel_id=(SELECT id FROM sn_config.channels WHERE slug='email'); "
            "INSERT INTO sn_config.agent_channels (agent_id,channel_id,instance_ref,enabled,history_before) "
            "SELECT " + str(peer["id"]) + ",id," + quote(box) +
            ",true,now()-interval '1 hour' FROM sn_config.channels WHERE slug='email'")
        status, sent = api("messages/inbound", "POST", {
            "recipient": box, "sender": "fixture@example.invalid", "channel_type": "email",
            "content": "E2E duplicate mailbox probe " + RUN,
            "metadata": {"subject": "Duplicate receiver probe", "history": False}})
        require(status == 200 and sent.get("id"), "incoming letter recorded")
        mid = sent["id"]
        messages.append(mid)
        status, resolution = api("messages/resolve-recipient?channel_type=email&recipient=" + box)
        require(status == 200, "real recipient-resolution API responds")
        check(resolution.get("agent") is None and resolution.get("reason") == "multiple-receivers",
              "mailbox resolution selects neither receiver")
        status, pending = api("agents/with-pending-work")
        require(status == 200, "real pending-work API responds")
        pending_slugs = {a["slug"] for a in pending["agents"]}
        check(not pending_slugs.intersection(slugs), "neither receiver is selected for a wake")
        for agent in agents:
            status, inbox = api("messages/inbox?limit=100", user=agent["os_user"])
            require(status == 200, "inbox read succeeds as " + agent["title"])
            require(isinstance(inbox.get("conversation"), list), "inbox returns a conversation")
            check(str(mid) not in {str(m["id"]) for m in inbox["conversation"]},
                  "letter is hidden from " + agent["title"])
        states = rows("SELECT status,error,processed_at FROM sn_core.message_state WHERE message_id=" + str(mid))
        events = rows("SELECT event_type,payload FROM sn_core.events WHERE entity_type='message' "
                      "AND entity_id=" + quote(mid) + " AND event_type='message.routing_denied'")
        print("  message state=" + json.dumps(states) + "; routing events=" + json.dumps(events))
        check(len(states) == 1 and states[0]["status"] == "blocked", "letter is blocked")
        check(len(states) == 1 and states[0]["processed_at"] is None,
              "letter is not marked processed")
        check(any(all(slug in json.dumps(e["payload"]) for slug in slugs) for e in events),
              "routing-denied event identifies both receivers")
        api("agents/with-pending-work")
        check(len(rows("SELECT id FROM sn_core.events WHERE entity_type='message' AND entity_id=" +
                       quote(mid) + " AND event_type='message.routing_denied'")) == len(events) and bool(events),
              "repeated polling does not duplicate the routing-denied event")
        check(rows("SELECT count(*) AS n FROM sn_config.agent_channels WHERE instance_ref=" + quote(box))[0]["n"] == 2,
              "both conflicting bindings remain available for diagnosis")
        require(not failures, "exclusive mailbox routing is enforced")


def john():
    matches = rows("SELECT a.id,a.slug,a.title,a.specialist_id,s.body_md FROM sn_config.agents a "
                   "JOIN sn_config.specialists s ON s.id=a.specialist_id WHERE a.archived_at IS NULL "
                   "AND s.slug='configurator'")
    require(len(matches) == 1 and matches[0]["title"] == "John", "one existing John configurator found")
    return matches[0]


def installed_release():
    return dict(line.split("=", 1) for line in
                Path("/etc/smart-node/release").read_text().splitlines() if "=" in line)


def onboarding():
    """An update delivers this checkout's configurator instructions to the existing John.

    Independent of release numbers: the baseline is whatever release the stand ran before the update
    (--capture-john), the expectation is the configurator source in the checkout under test."""
    require(os.environ.get("E2E_ONBOARDING_BASELINE"),
            "E2E_ONBOARDING_BASELINE names the file --capture-john wrote before the update")
    baseline = json.loads(Path(os.environ["E2E_ONBOARDING_BASELINE"]).read_text())
    source = (REPO / "companions/srv-skeleton/agents/_specialists/configurator.md").read_text()
    expected = source.split("---", 2)[2].strip() + "\n"
    current = john()
    stamp = installed_release()
    require(baseline["commit"] != stamp.get("commit"),
            "the baseline was captured before the update (its commit differs from the installed one)")
    if baseline["body_sha256"] == hashlib.sha256(expected.encode()).hexdigest():
        print("  NOTE: the baseline John already carried these instructions; the update had nothing to deliver")
    require(all(current[k] == baseline[k] for k in ("id", "slug", "specialist_id")),
            "existing agent and specialist identities survive the update")
    prompt = Path("/srv/agents", current["slug"], "CLAUDE.md").read_text()
    print("  DB body sha256=" + hashlib.sha256(current["body_md"].encode()).hexdigest())
    failures = []
    for condition, message in [(current["body_md"] == expected, "database contains this checkout's complete configurator body"),
                               (expected in prompt, "existing John's CLAUDE.md contains this checkout's complete configurator body")]:
        try:
            require(condition, message)
        except AssertionError as error:
            print("  FAIL:", error)
            failures.append(str(error))
    require(not failures, "update delivered this release's instructions to the existing John")


def main():
    require(os.geteuid() == 0, "running node-local as root")
    require(os.environ.get("SN_E2E_DISPOSABLE") == "1" and
            re.match(r"^(sn-|stand-)", socket.gethostname()), "explicit disposable stand selected")
    if sys.argv[1] == "--capture-john":
        existing = john()
        baseline = {k: existing[k] for k in ("id", "slug", "title", "specialist_id")}
        stamp = installed_release()
        baseline["release"] = stamp["release"]
        baseline["commit"] = stamp["commit"]
        baseline["body_sha256"] = hashlib.sha256(existing["body_md"].encode()).hexdigest()
        Path(sys.argv[2]).write_text(json.dumps(baseline, indent=2) + "\n")
        print(json.dumps(baseline))
        return
    scenario = sys.argv[1]
    print(f"[e2e] release-candidate: scenario={scenario} run={RUN}", flush=True)
    try:
        globals()[scenario]()
    except (AssertionError, OSError, KeyError, subprocess.SubprocessError, ValueError) as error:
        print("  FAIL:", str(error), flush=True)
        print("[e2e] RESULT: FAIL")
        sys.exit(1)
    print("[e2e] RESULT: PASS")


if __name__ == "__main__":
    main()
