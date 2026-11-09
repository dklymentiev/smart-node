# Human identity on the core-api TCP door

core-api and admin-runner refuse a request that changes something (any method
other than GET, HEAD or OPTIONS) when its only identity is a `Remote-User`
header naming a person or an administrator. A trusted source address says where
a packet came from, not who set its header: every agent on the node calls from
the same trusted network.

**There is no switch. Deploying this code enables the refusal.** A flag that
defaulted to off would ship a release with the hole open, and one that defaulted
to on would change nothing. So every caller the node itself runs has to arrive
by a proved path in the same release, and the list below is that set.

Reads are not affected: a safe method by an asserted name answers as before.
Each refusal answers 403 and is written to the event log as
`auth.human_tcp_assertion_denied`, with the method and path.

## What counts as proof

| Proof | Who can produce it |
| --- | --- |
| Proxy session header (`X-SN-Proxy-Auth`) | The reverse proxy, for a signed-in browser session; Studio relays it |
| Unix socket peer (`SO_PEERCRED`) | The kernel. An agent's OS user is that agent; root, or a registered operator account, is the node owner |
| Actor bearer token | An operator the owner issued a credential to |
| Service credential (`X-SN-Service`, `X-SN-Service-Auth`, `X-SN-On-Behalf-Of`) | A node service, for the calls listed for it and for nobody but the configured owner |

## Callers and how each is proved

| Caller | Call | Proof |
| --- | --- | --- |
| Browser, through the panel or Studio | everything a person does | proxy session header |
| Installer, `bootstrap/onboarding-seed.sh` | first agent, its grants, the first task, the owner desk | unix socket as root |
| Host maintenance: cron reconcile, retention, update and acceptance scripts | their own routes | unix socket as root, or the proxy proof where a session is required |
| Planner, `planner/notify.py` | `POST /api/notify` | service credential, service `planner` |
| Dispatcher message watcher | `POST /api/messages/{id}/deliver-desk`, `PATCH /api/messages/{id}/state` | service credential, service `brin` |
| Dispatcher runner | `POST /api/notify`, `POST /api/messages/reply`, `POST /api/integrations/google/calendar/events` | service credential, service `brin` |
| Dispatcher runner, broadcast sender | `POST /api/messages/send` as the sending agent | service credential delegated to an agent |
| Gateway budget and turn cutoff notices | three notice routes | the gateway's own credential |
| core-api to admin-runner | every privileged operation | the core-api client credential, original caller in `X-SN-On-Behalf-Of` |

The planner and dispatcher lists above are the whole of what the service
credential opens for the owner. The same proof on any other route is refused,
as is a name other than the configured owner, and a delegated call is recorded
as `identity.delegated_owner` with the service that made it. A delegated owner
is `proved`, never `session`: an approval still needs a signed-in person.

The runner releases a reply or creates a calendar event only after it has read
the owner's approval comment in the planner. core-api does not see that comment;
it trusts the dispatcher to have checked it.

### One credential, several holders

The planner, the dispatcher and the voice service present the same
`SN_SERVICE_AUTH_SECRET`. The service name in the request is therefore a claim,
not a proof: any holder can present itself as any listed service. The per-service
lists limit what the credential opens in total. They do not separate one holder
from another.

## The owner contract

`/etc/smart-node/operator.env` carries one line, `SN_OPERATOR_ACTOR`, the
identity of the node owner. `companions/core-api.sh` writes it on every run,
full or contracts-only, by reading the active human super-admin from the users
table. A value already in the file is kept while that account is still an active
human super-admin; with several such accounts and no file, the oldest is taken.
A node with none stops the companion with a message.

It is read by three units, each through `EnvironmentFile=-`:

| Unit | Also needs | From |
| --- | --- | --- |
| core-api | `SN_SERVICE_AUTH_SECRET` | `core-api-service-auth.env` |
| planner (tracking companion) | `SN_SERVICE_AUTH_SECRET` | `core-api-service-auth.env` |
| dispatcher and runner | `SN_SERVICE_AUTH_SECRET` | the dispatcher's own `.env`, copied at install |

There is no built-in owner name anywhere. If a setting is missing, the call
fails where it can be seen:

- core-api answers 503 naming `SN_OPERATOR_ACTOR` or `SN_SERVICE_AUTH_SECRET`,
  and logs the same;
- the planner logs `notify NOT sent: <setting> not set` for every notify, and
  `notify REFUSED` with core-api's answer when a call is turned away;
- the dispatcher logs `<setting> not set in the service environment` and does
  not make the call;
- root on the socket resolves to nobody and is refused 403.

## Update order

The standard update runs the companions in dependency order: core-api first,
then tracking, then dispatcher. core-api writes the owner contract before it
restarts with the refusal in force; the other two are restarted by their own
companions and pick the file up. Between the core-api restart and the tracking
and dispatcher restarts, a notify or a desk delivery is refused and logged; the
watcher retries a desk delivery, a task notification in that window is not
resent.

The three companion stage versions are raised with this change, so an update
installs the new code instead of only rewriting contracts.

## What to check after an update

1. `/etc/smart-node/operator.env` exists and names the owner account.
2. `systemctl show -p EnvironmentFiles` lists it for the core-api, planner,
   dispatcher and runner units.
3. Assign a task to an agent. The planner log shows `notify: ... -> 200` and the
   agent wakes.
4. Send a message in a Studio desk. The dispatcher log shows the desk message
   delivered, not `answered 403`.
5. `auth.human_tcp_assertion_denied` in the event log is empty, or names only
   callers you know to be outside the node's own services. Each row carries the
   path that was refused.
6. A bare owner header is refused:
   `curl -X POST -H 'Remote-User: <owner>' http://127.0.0.1:<port>/api/desks`
   answers 403.

## Before this is released

- core-api refuses every route that has not declared a permission. Until the
  route declarations are in the same release, the API answers 403 on those
  routes whatever the identity. This change does not add them.
- The source test `tests/human-tcp-proof.test.sh` runs the real callers against
  core-api's routers on a throwaway database. It cannot stand in for one clean
  install and one update of an existing node: the installer steps that write the
  owner contract and the units need root and a real node.

## Known gaps

- Gateway notices name the owner through `BUDGET_NOTIFY_ACTOR`, which the
  communication companion writes as a fixed name. On a node whose owner account
  carries another name those notices are refused.
- The dispatcher installer looks up the first agent with a fixed owner name on
  a read. A read is not refused; on a node whose owner carries another name the
  lookup finds nothing and the watcher resolves its agent at run time instead.
- The manual scripts under `companions/brin/tests/` assert the owner by header
  and are refused. They are not run by the product.
