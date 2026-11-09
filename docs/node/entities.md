# What exists, and how to change it

One row per thing the node is made of. `Owner says` is the business phrasing you
will hear. `Call` is what you actually do. `Gate` says whether the owner must
approve first. `Proved by` is what makes it true, as opposed to merely returned.

**This table is the source of the calls quoted in `node-versioning/capabilities.md`**,
which is the list that travels into every agent's prompt. Change a call here
first; `tools/capabilities-check.sh` fails the moment the two disagree.

Base URL for every call below: `http://127.0.0.1:8030`. Identity travels in the
`Remote-User` header.

## The table

| Thing | Owner says | Call | Gate | Proved by | Lives in |
|---|---|---|---|---|---|
| **Agent** | create a worker, name it, give it a role | `POST /api/agents` {slug,title,role,model,specialist_slug or specialist_body} | yes | it appears in `GET /api/agents` AND answers a message | `sn_config.agents` + `/srv/agents/<slug>` |
| | change it | `PUT /api/agents/{slug}` | no, for yourself | read the field back |  |
| | retire it | `DELETE /api/agents/{slug}` (soft) | yes | |  |
| | rebuild its instructions after any change | `POST /api/agents/{slug}/regenerate` | no | `GET /api/agents/{slug}/system-prompt` shows the change |  |
| **Persona** | define a role others can take; read a ready-made one | `POST /api/specialists` {slug,name,body_md}; `GET /api/specialists/library`, `GET /api/specialists/library/{slug}` | no | an agent created with it carries the text | `sn_config.specialists` |
| **Contact** | add a person or organisation to the address book | `POST /api/contacts` | no | listed in `GET /api/contacts`; creation alone grants nobody permission | `sn_config.contacts` |
| | allow an agent to write to that contact on named channels | `PUT /api/agents/{slug}/contacts/{contact-id}` {channels:[channel_slug]} | owner | listed in `GET /api/agents/{slug}/contacts` and reflected in its prompt | `sn_config.agent_contacts` |
| | revoke that permission | `DELETE /api/agents/{slug}/contacts/{contact-id}` | owner | absent from the agent's contacts | |
| **Department** | group agents | `/api/departments` | no | | `sn_config.departments` |
| **Tool** | register a capability | `POST /api/tools` {name,kind,title,description,usage_md} | no | it lists in `GET /api/tools` | `sn_core.tools` |
| | give an agent a capability | `PUT /api/agents/{slug}/grants` {grants:{tool:mode}} | yes | the agent's prompt contains the tool contract, and it can run the tool | `sn_config.agent_tools` |
| **Channel** | let people reach an agent this way | `PUT /api/agents/{slug}/channels/{channel_slug}` {instance_ref,enabled} | no if the channel already works; yes if it needs a new mailbox or bot credentials | a real message in and a real reply out | `sn_config.channels` + `sn_config.agent_channels` |
| **Integration** | connect an outside service or a channel provider | `POST/PUT/DELETE /api/integrations/{slug}` | yes | the provider answers | `sn_config.integrations` |
| **Knowledge** | let this agent know this | `POST /api/agents/{slug}/knowledge` (kinds: text, doci, url, gdrive_file) | no | it appears in the agent's context | `sn_config.agent_knowledge` |
| **Memory** | let it remember across conversations | `PUT /api/agents/{slug}` field `mesh_workspace` | no | a written note is found by a later search | mesh workspace named on `agents.mesh_workspace` |
| **Policy** | set a rule | `PUT /api/agents/{slug}/policies/{policy_slug}` | no for one agent, yes node-wide | the rule appears in the prompt AND the forbidden action is refused | `sn_config.agent_policy_opts` |
| | a rule for the whole node | `POST /api/policies` | yes | | `sn_config.policies` |
| **Schedule** | run a system command on a timer (the sysadmin's job; agents are woken by the Runner, and a reminder is how an agent acts at a time) | `POST/PUT/DELETE /api/crons` | no | the command runs, in `/var/log/smart-node/cron/<job>.log` | `sn_core.crons` -> `/etc/cron.d/` |
| **Message** | send, reply, receive | `/api/messages/...` | depends on content | the message arrives at the far end | `sn_core.messages` (+ `sn_core.message_state`) |
| **Task** | track a piece of work | the node's planner CLI | no | the task exists and its state changes | the node planner's own database |
| **Document** | write it down | the node's document system | no | it reads back | the node's document system |
| **Model** | which brain an agent uses; add one to the catalog | `GET /api/models/providers` to look, `POST /api/models` to add, `model` (`<provider>/<model>`) on the agent | yes (owner, operator, configurator) | the agent answers using it | `sn_config.models` / `agents.model` |
| **MCP server** | give it a tool server | `/api/mcp/servers` | yes | the tool appears in the agent's session | `sn_config.mcp_servers` |
| **Secret** | store a credential | `GET /api/secrets/refs`, `POST /api/secrets` {ref,value} with a dotted ref such as `replicate.api_key` (the gateway's keys are `gateway/<provider>-key`), `DELETE /api/secrets/{ref}`, `GET /api/secrets/resolve`, `GET /api/secrets/requests` | yes | the component using it works; the Secrets page shows who read it | `sn_config.secrets`, pgp-encrypted with the node secret key `/etc/smart-node/gateway-secret.key`; earlier values in `sn_config.secret_versions`. One store for the whole node. The routes are the only write path |
| **Desk** | a place to talk to an agent | `/api/desks` | no | | `sn_core.desks` |
| **Project** | a work area a few of them share | `POST /api/projects` {slug,name,planner_project} then `POST /api/projects/{slug}/members` | yes -- a signed-in session, not an asserted identity | a member agent writes a file there and a NON-member is refused | `sn_config.projects` + `/srv/projects/<slug>` (2770 setgid); memory `project-<slug>`; documents `<slug>/` |
| **Workflow** | run a defined chain of specialists | `GET /api/workflows`, `POST /api/workflows/{name}/run`, `POST /api/workflows/import` | yes | the run produces its output, not merely a started status | `sn_core.workflows` + `sn_config.agent_workflows` (the grant). Registered from a flow YAML under `/opt/smart-node/flows/agents/flows` by `register-workflows.py` |
| **Node state** | what release is this, what is running | `GET /api/system/status` (also `/overview`, `/services`) | no | a service it reports as running answers on its own port | `sn_core.node_services` |
| **Approval gate** | approve, reject | cross-cutting, not one endpoint | | | a `comment_type=approval` comment on the task |

## Four that trip people

**A channel is two things.** The *type* (email, telegram, voice) must be enabled
on the node, and the *binding* says which agent owns which address. Enabling the
type alone routes nothing: mail arrives, nobody owns it, nothing answers. The
binding is `agent_channels`, and `instance_ref` is the address itself: a mailbox
for email, a bot name for telegram, empty for voice.

**Email needs something the node cannot make.** The mailbox lives on a mail
server the node does not run. Binding an address that does not exist yet produces
a configuration that looks complete and delivers nothing. Ask the owner for the
mailbox first, then bind it.

**A tool is registered once and granted many times.** Registering puts it in the
catalogue; granting is what lets one agent use it. Its `usage_md` is injected
into the prompt of every agent granted it, so write it at registration.

**Per-agent path restrictions are not enforced.** `fs_read`, `fs_write` and
`forbidden_fs` reach the agent as text in its prompt. They are an instruction, not
a boundary. Do not describe them to the owner as protection.

## Where the edges are

The node cannot create a mailbox, change DNS, mint a bot token, or spend money.
Those come from the owner. When one blocks you, say exactly which, and what you
will do the moment you have it.
