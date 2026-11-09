# Smart Node — Capabilities

> What a node of this release can do, in the owner's language.
> **Release:** stamped from the build at release time (draft: built against the current tree).
> A capability here is what the *version* ships. Whether it is switched on is a
> per-node overlay — an entry with no *"Switch on:"* clause works out of the box;
> an entry with one is in the version but needs that one thing on this node.
> The configurator reads this as fact: it proposes only from here, and never
> promises anything not on this list.
>
> **Each entry carries the call that performs it and whether the owner must
> approve first.** They are in HTML comments: invisible to a person reading the
> rendered page, present in the agent's prompt, which is where they are needed.
> Knowing the capability without the call is what makes an agent search instead
> of act.
>
> **`docs/node/entities.md` in the installer repository is the source of those
> calls.** Do not edit a call here on its own; change it there and let
> `tools/capabilities-check.sh` catch the disagreement. Base URL for every call
> below: `__CORE_API_URL__`, identity in the `Remote-User` header.

## Agents & structure
- **Agents** — AI workers with a name and a role; create them, instruct them, set their rules. Create, change and remove them with your `create_agent`, `update_agent` and `delete_agent` tools. <!-- by: core-api --> <!-- call: POST /api/agents, PUT /api/agents/{slug}, DELETE /api/agents/{slug}, POST /api/agents/{slug}/regenerate | gate: owner to create or retire; none to change one that already exists; regenerate after every change -->
- **Roles** — a reusable role (persona) an agent takes on. <!-- by: core-api --> <!-- call: POST /api/specialists | gate: none -->
- **Departments** — group agents by area of work. <!-- by: core-api --> <!-- call: /api/departments | gate: none -->
- **Rules** — a rule an agent must follow, for one agent or for the whole node. <!-- by: core-api --> <!-- call: PUT /api/agents/{slug}/policies/{policy_slug}, POST /api/policies | gate: none for one agent; owner for a rule that binds the whole node -->
- **Admin agent (the owner's hands)** — the owner speaks plainly ("put the sales agent on email", "make a workflow"); safe things run at once, sensitive ones go to the owner for approval. <!-- by: node-admin --> <!-- call: PUT /api/agents/{slug}/grants | gate: owner -->

## Work
- **Tasks** — plan and track work. <!-- by: planner --> <!-- call: the node's planner CLI | gate: none -->
- **Documents & documentation** — create, version, search and discuss documents (including with an agent). <!-- by: doci --> <!-- call: the node's document system | gate: none -->
- **Memory** — accumulate what the node knows about the business, in separate areas, searchable by meaning. <!-- by: mesh --> <!-- call: PUT /api/agents/{slug} field mesh_workspace, POST /api/agents/{slug}/knowledge | gate: none -->
- **Workflows** — automatic chains of specialists, with owner approval. <!-- by: workflows --> <!-- call: GET /api/workflows, POST /api/workflows/{name}/run, POST /api/workflows/import | gate: owner -->
- **Scheduled actions** — recurring and deferred actions. <!-- by: crons --> <!-- call: POST/PUT/DELETE /api/crons | gate: none -->

## Reach & files
- **Read web pages** — read a page by its link. <!-- by: fetch --> <!-- call: PUT /api/agents/{slug}/grants {grants:{fetch:...}} | gate: owner -->
- **Work with files.** <!-- by: fs --> <!-- call: PUT /api/agents/{slug}/grants {grants:{fs:...}} | gate: owner -->

## Communication (channels)
- **Web chat** — talk to an agent in a browser. <!-- by: panel --> <!-- call: /api/desks | gate: none -->
- **Internal messaging** — agents write to each other and to people; a message is never lost and must be answered. <!-- by: brin --> <!-- call: /api/messages/... | gate: depends on what is being sent -->
- **Email** — receive and send mail as a channel. *Switch on: a mailbox.* <!-- by: communication --> <!-- call: PUT /api/agents/{slug}/channels/{channel_slug} with instance_ref = the mailbox | gate: owner, because the mailbox is made on a mail server the node does not run; ask for it before binding -->
- **Voice** — talk to an agent by voice. *Switch on: a Gemini key.* <!-- by: voice --> <!-- call: PUT /api/agents/{slug}/channels/{channel_slug} with instance_ref empty | gate: owner, the key comes from them -->
- **Telegram** — a Telegram channel to the business. *Switch on: a bot token.* <!-- by: communication --> <!-- call: PUT /api/agents/{slug}/channels/{channel_slug} with instance_ref = the bot name | gate: owner, only they can mint the token -->
- **Contacts** — the address book and per-agent permission to write to named people on specified channels. Creating a contact grants it to nobody; grant it to the agent with the owner's approval. <!-- by: contacts --> <!-- call: POST /api/contacts, PUT /api/agents/{slug}/contacts/{contact-id}, DELETE /api/agents/{slug}/contacts/{contact-id} | gate: owner to grant -->

## For the owner
- **Credentials** — keep a key or a password for the node to use, encrypted, and hand it to whichever component needs it. <!-- by: core-api --> <!-- call: POST /api/secrets, GET /api/secrets/refs, DELETE /api/secrets/{ref} | gate: owner, it is their credential -->
- **Node administration** — run commands, manage services, read the node's state. <!-- by: node-admin --> <!-- call: PUT /api/agents/{slug}/grants {grants:{node-admin:...}} | gate: owner -->
- **Self-description** — the node knows its own release and reports if its make-up changed. <!-- by: versions --> <!-- call: GET /api/system/status | gate: none -->

## Can be added, not working until set up
Say what it gives and what setting it up takes; never present it as ready, never as impossible. Where no connector exists, offer to have one built: first create a developer agent for it, once the owner agrees.
- **Google Drive** — an integration that comes with Smart Node (`gdrive`): install it, the owner signs in to Google.
- **Slack** — a real connector exists, not yet in the release build.
- **Gmail, Google Calendar** — a connector (an MCP server) to install and set up; the owner signs in.
- **Telephony** — answering or placing calls on a real phone number: a connector to a phone service.
- **Posting to social media** — a connector for that network, with the owner's account.

---
Two axes this list carries:
- **Capability vs. right.** This is what the node *can* do. *Who* may switch on
  sensitive things is a separate axis: the owner (a person) is root; the admin
  agent is their delegated hands; the team are function agents with limited
  rights. Switching on a channel, deleting an agent, or spending money goes
  through the owner gate.
- **Version vs. node.** This block is tied to the release and travels with it
  (`smart-node-docs` tagged `node-<release>`; the agent reads the snapshot baked
  into its own version). The "Switch on" overlay is computed per node.
