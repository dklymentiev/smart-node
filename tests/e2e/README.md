# Release candidate regressions

Run these tests **inside an installed disposable stand as root**. They exercise
the live core-api, PostgreSQL, admin-runner, runner and gateway. They temporarily
start or stop services and restore their previous active state on exit. Test
agents are disabled and archived through the API, and only fixture message rows
are removed; audit events remain available for diagnosis. Never use a production
node. `SN_E2E_DISPOSABLE=1` and a stand hostname (`sn-*` or `stand-*`) are required.

Operator requests use the installed core-api's trusted loopback endpoint and
owner identity, resolved from the stand database. This works on both releases;
agent inbox reads use the Unix socket under each agent's actual OS identity.

Copy the test checkout onto the stand as test tooling; do not replace installed
service files. On the baseline, run the tests from the candidate checkout against
the services still installed from the previous release. Both runs must use the same
regression assertions. Record checkout commits and any harness
readiness correction with the evidence.

Before updating, capture the existing John on the release the stand runs now:

```sh
export SN_E2E_DISPOSABLE=1
python3 tests/e2e/release_candidate.py --capture-john /tmp/e2e-john-before.json
export E2E_ONBOARDING_BASELINE=/tmp/e2e-john-before.json
bash tests/run-e2e.sh --evidence-dir /tmp/e2e-red \
  --only agent-create-immediate-message.sh \
  --only agent-create-rollback.sh \
  --only mailbox-exclusive-receiver.sh \
  --only onboarding-v6-update.sh
```

On a previous release that lacks the fixes the first three fail; the onboarding scenario only has
something to deliver while the previous release carries older instructions than this checkout (it prints
a note when it does not). Update that stand through the normal
installer from the reviewed main commit, then rerun the same command with
`--evidence-dir /tmp/e2e-green`. All four must pass, whatever the release numbers. Keep the baseline JSON,
installer log, runner output from both runs and their exit statuses with the MR.
The selective runner continues after a failed scenario and writes passing
evidence to `DATE-selected.json`; this is not evidence of the full e2e suite or
operator acceptance.

- Creation sends an internal message within one second of the API's 201 response,
  then observes an actual Claude process running as the new OS user from the
  agent's directory. The stand needs a working Claude CLI account; provider or
  service failures are failures, never skips. Override `E2E_CREATE_MODEL` for
  another installed `claude-cli/` model, and `E2E_START_TIMEOUT` for a slower stand.
- Rollback stops admin-runner and checks the error response, every agent row
  (including archived rows), identity, inline specialist, bindings and directory.
- Mailbox routing gives Hazel a reserved test address, checks a second API bind
  is refused with her name, and inserts the duplicate directly into PostgreSQL.
  The real APIs must resolve no receiver, exclude both from wakes, hide the mail
  from both OS-authenticated inboxes, and record a routing-denied event while
  leaving `processed_at` empty and both conflicting bindings intact. Mailbox
  fixtures explicitly provision their OS identities through admin-runner so the
  baseline failure measures routing rather than the separate creation defect.
- Onboarding reads the existing configurator's database body and `CLAUDE.md`
  without regenerating or repairing them. It compares the complete reviewed v6
  body and verifies that agent and specialist identities match the old baseline.
