# DOCI — Vision

## Problem

Knowledge work generated with AI assistants does not live well in either of
the two dominant homes:

- **Notion / Confluence / GitBook.** Closed storage, no git, no sane diffs.
  Every "history" feature is a proprietary timeline. Agents can read and
  write the pages only through a vendor SDK that lags behind the model.
- **Raw git repositories.** Markdown lives in a folder, but the folder has
  no titles, tags, summaries, threads, or stable IDs. Renaming a file
  breaks every link to it. There is no UI for non-developers.

The user lands on each side, copies content between them, and pays the cost
in stale duplication, broken references, and lost provenance.

## What DOCI is

DOCI is a small self-hosted document workspace that takes:

- A **git working tree** as the source of truth for content
  (`files/*.md`).
- A **PostgreSQL table** as the index for metadata (GUID, path, title,
  parent, tags, version chain, soft delete).
- A **web UI**, a **REST API**, a **CLI** (`deep`), and an **MCP server**
  (`mcp_server.py`), all sitting on the same repository layer.

A document keeps its stable GUID across renames. Threads attach to documents
(and to specific quoted passages). Versions snapshot a document before each
edit. AI agents drive the whole thing through MCP — the same tools the UI
calls.

## What DOCI is NOT

- Not a **task tracker**. See sibling project
  planner for that.
- Not a **wiki**. There is no in-app templating, no permission matrix per
  page, no team workspaces. One repo per instance.
- Not a **vector database**. Optional integration with
  [mesh-memory](https://github.com/dklymentiev/mesh-memory) provides
  semantic search; without it, only path/tag/title search.
- Not a **block editor**. Documents are plain markdown. The UI renders;
  it does not WYSIWYG.

## Audience

- **Solo operators** who already script their workflow with AI agents and
  want to capture the artifacts somewhere git-clean and queryable.
- **Small teams** (1-5 people) who can share a single git repo and a
  single Postgres database, and who prefer markdown over Notion.
- **Tool builders** wiring up agents that need a place to write and read
  back markdown without parsing a UI.

## Value

- **Provenance.** Every change is a git commit. `git log -- files/<path>`
  gives the full history; `git show` gives the diff.
- **Stable identity.** GUID stays put across renames; links don't rot.
- **One API surface.** UI, CLI, REST, and MCP are the same operations.
  Whatever an agent can do, a human can do, and vice versa.
- **Composable with siblings.** Plays well with mesh-memory (semantic
  search) and planner (task layer); independent of either.

## Non-goals

- No real-time collaboration. Last-writer-wins; merges resolved in git.
- No client-side rich-text editor. Markdown is the contract.
- No multi-tenant isolation. One installation = one repo.
- No model hosting. AI features expect an external chat-completion gateway.

## Success criteria for v1.0

- A new user can clone, `docker compose up`, and have a working UI in
  under five minutes.
- The same set of operations is accessible through UI, REST, CLI, and
  MCP — no operation lives in only one of them.
- A document's history (versions + git commits) is sufficient to
  reconstruct any past state of the file and its metadata.
- A solo developer can host an instance on a $5/mo VPS without external
  paid services beyond the AI gateway.
