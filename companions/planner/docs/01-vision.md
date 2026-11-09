# Planner -- Vision

**Status:** Draft, v0.1
**Document:** 01-vision (per Software Product Template v1.1)
**Date:** 2026-04-27

---

## Problem Statement

Small engineering teams and solo developers need structured task management with a real lifecycle -- intake, assessment, approval, execution, verification, delivery -- not just a kanban board with three columns. Existing options force a choice: lightweight tools (Todoist, Trello, Linear) skip the assessment and approval phases entirely, while enterprise platforms (Jira, ServiceNow) require a dedicated administrator and weeks of configuration. Worse, almost none of them expose first-class machine interfaces -- REST APIs are second-class citizens, and MCP (Model Context Protocol) for AI agents does not exist. The result: teams either underuse process and accumulate incidents, or get crushed under enterprise overhead, and AI agents have no structured place to plan and track their own work.

## Solution

Planner is a self-hostable task manager built around a six-stage pipeline (intake -> assessment -> approval -> execution -> verification -> delivery) with hierarchical objectives (strategy / roadmap / milestone / goal / initiative / campaign) above the task level. It runs on PostgreSQL and exposes a REST API, an MCP server, a CLI, and a thin web UI as equally first-class interfaces -- every operation is available everywhere. A single `docker compose up` deploys the entire stack on a laptop or server in under five minutes.

## Target Users

| Persona | Description | Key Need |
|---|---|---|
| Indie developer | Solo or 1-3 person team, ships products | Lightweight task store with audit log, MCP for AI agents |
| Small engineering team | 5-15 people, no dedicated PM role | Pipeline with explicit approval gates, no admin overhead |
| AI-agent operator | Runs autonomous agents on own infrastructure | Structured task store agents can read/write via MCP and REST |
| Agency / consultancy | Manages multiple parallel client projects | Project hierarchy, per-project filters, audit trail per client |

## Value Proposition

Teams choose Planner over alternatives because it is the only self-hostable open-source task manager that combines a proper assessment-approval-verification pipeline (not just kanban columns) with first-class machine interfaces -- REST, MCP, CLI, and UI on equal footing -- and deploys in under five minutes on a single PostgreSQL instance.

## Non-Goals

- NOT a document storage or version-control system. Planner stores opaque external link fields (`plan_url`, `worklog_url`, `external_ref`) and lets the user point them at any system.
- NOT a memory or knowledge base. No semantic search, no embeddings, no full-text retrieval beyond simple title/description search.
- NOT an AI agent orchestrator. No workflow engine, no agent definitions, no LLM calls of its own.
- NOT a chat or messaging platform. Comments are simple plain-text records attached to tasks.
- NOT a portfolio-management tool for executives -- no Gantt charts, no resource allocation, no financial forecasting.
- NOT a replacement for Jira / ServiceNow in organisations of more than ~50 people with formal change-management processes.
- Will NOT offer hosted SaaS in v1 -- self-hosted only.

## Success Criteria

| Metric | Target | Timeframe |
|---|---|---|
| Time from `docker compose up` to first task created | < 5 minutes | At v1.0 release |
| Feature parity across CLI / REST / MCP / UI | 100% (no UI-only feature) | At v1.0 release |
| Cold-start memory footprint | < 256 MB | At v1.0 release |
| GitHub stars | > 200 | 6 months post-release |
| Active self-hosted instances (opt-in telemetry) | > 50 | 6 months post-release |
| Median issue first-response time | < 72 hours | Steady state |

## Competitive Landscape

| Competitor | Strength | Weakness | Our Differentiation |
|---|---|---|---|
| Linear | Fast UI, dev-centric | SaaS only, no pipeline gates, no MCP | Self-hosted, approval pipeline, first-class MCP |
| Jira | Enterprise features, mature ecosystem | Heavy admin overhead, slow, expensive | Five-minute install, no admin role required |
| Plane.so | Open-source, modern UI | Mimics Linear (no pipeline), early MCP support | Pipeline-first, MCP as core interface, not addon |
| OpenProject | Mature OSS, full feature set | Heavy stack, complex setup, no MCP | Lightweight install, agent-native by design |
| Vikunja / Wekan | Lightweight self-hosted | Just kanban, no pipeline, no MCP | Pipeline + agent interfaces |
| GitHub / GitLab Issues | Free, native to code | Tied to a repo, weak hierarchy, no gates | Repo-agnostic, six-stage lifecycle, objective tree |

## Deployment Modes

Planner supports two equally-first-class modes:

1. **Standalone.** A single `docker compose up` deploys Planner together with its own PostgreSQL on a laptop or VPS. All four interfaces (CLI, REST, MCP, UI) are immediately available. This is the default for individuals and small teams.

2. **Embedded.** Planner is installed as a component of a larger system, configured to share that system's existing PostgreSQL instance using its own schema namespace, and to consume that system's design tokens for the UI. The standalone UI can be replaced by the host system's UI consuming Planner's REST or MCP, while the Planner core itself is unchanged. This is the integration mode for larger platforms.

Both modes use the same release artifact; configuration controls behaviour.

---

> Next: [02-spec.md](02-spec.md) -- turn this vision into concrete use cases and features.
