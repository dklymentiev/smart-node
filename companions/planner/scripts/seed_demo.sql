-- Demo seed: a coherent strategy -> roadmap -> milestone -> goal -> initiative -> campaign
-- hierarchy with tasks attached at multiple levels. Re-runnable: wipes prior demo rows first.

BEGIN;

-- =========================================================================
-- 0. Clean prior demo data (keep 'system' actor + root api_key)
-- =========================================================================
DELETE FROM task_notifications;
DELETE FROM task_links;
DELETE FROM task_tags;
DELETE FROM task_log;
DELETE FROM task_commits;
DELETE FROM task_relations;
DELETE FROM task_comments;
DELETE FROM tasks;
DELETE FROM objectives;
DELETE FROM projects;
DELETE FROM api_keys WHERE label <> 'root';
DELETE FROM actors  WHERE id <> 'system';

ALTER SEQUENCE projects_id_seq           RESTART WITH 1;
ALTER SEQUENCE objectives_id_seq         RESTART WITH 1;
ALTER SEQUENCE tasks_id_seq              RESTART WITH 1;
ALTER SEQUENCE task_comments_id_seq      RESTART WITH 1;
ALTER SEQUENCE task_relations_id_seq     RESTART WITH 1;
ALTER SEQUENCE task_commits_id_seq       RESTART WITH 1;
ALTER SEQUENCE task_log_id_seq           RESTART WITH 1;
ALTER SEQUENCE task_links_id_seq         RESTART WITH 1;
ALTER SEQUENCE task_notifications_id_seq RESTART WITH 1;

-- =========================================================================
-- 1. actors
-- =========================================================================
INSERT INTO actors (id, display_name, type, active, created_at) VALUES
    ('alice',  'Alice Carter', 'human', true,  now() - interval '120 days'),
    ('bob',    'Bob Nguyen',   'human', true,  now() - interval '90 days'),
    ('claude', 'Claude Agent', 'agent', true,  now() - interval '60 days'),
    ('eve',    'Eve Retired',  'human', false, now() - interval '200 days');

-- =========================================================================
-- 2. api_keys
-- =========================================================================
INSERT INTO api_keys (key_hash, label, actor_id, created_at, last_used_at, revoked_at) VALUES
    (encode(sha256('demo_key_alice'::bytea),  'hex'), 'alice-laptop', 'alice',  now() - interval '30 days', now() - interval '2 hours', NULL),
    (encode(sha256('demo_key_bob'::bytea),    'hex'), 'bob-ci',       'bob',    now() - interval '25 days', now() - interval '1 day',   NULL),
    (encode(sha256('demo_key_claude'::bytea), 'hex'), 'claude-agent', 'claude', now() - interval '15 days', now() - interval '5 minutes', NULL),
    (encode(sha256('demo_key_revoked'::bytea),'hex'), 'old-mobile',   'alice',  now() - interval '60 days', now() - interval '50 days', now() - interval '45 days');

-- =========================================================================
-- 3. projects (3 active + 1 archived)
-- =========================================================================
INSERT INTO projects (slug, title, description, status, sort_order, created_at, updated_at) VALUES
    ('planner-core',  'Planner Core',
     'Self-hostable task manager. The product itself.',
     'active',   1, now() - interval '180 days', now() - interval '1 day'),
    ('client-portal', 'Client Portal',
     'Customer-facing read-only portal.',
     'active',   2, now() - interval '90 days',  now() - interval '3 days'),
    ('mobile-app',    'Mobile App',
     'iOS / Android companion.',
     'active',   3, now() - interval '30 days',  now() - interval '5 days'),
    ('legacy-api',    'Legacy API',
     'Old REST surface, sunset planned.',
     'archived', 9, now() - interval '500 days', now() - interval '150 days');

-- =========================================================================
-- 4. objectives
-- =========================================================================
-- Project 1 (planner-core) — full hierarchy
-- Strategy #1
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (NULL, 1, 'Best-in-class self-hostable planner', 'strategy', 'active', 1,
     'Multi-year direction. Beat Linear/Jira on simplicity, beat Trello on power.',
     now() - interval '180 days', now() - interval '7 days');
-- Roadmap #2 under strategy #1
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (1, 1, 'v1.0 General Availability', 'roadmap', 'active', 1,
     'First production-ready release. REST + Web UI + MCP + CLI all stable.',
     now() - interval '170 days', now() - interval '5 days');
-- Milestone #3: R0 Foundation [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (2, 1, 'R0: Schema + REST foundation', 'milestone', 'completed', 1,
     '12-table schema, 5 routers, OpenAPI auto-published.',
     now() - interval '160 days', now() - interval '120 days');
-- Goal #4 under milestone #3 [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (3, 1, 'REST surface frozen + OpenAPI live', 'goal', 'completed', 1,
     'No more shape changes; downstream clients safe to integrate.',
     now() - interval '155 days', now() - interval '125 days');
-- Initiative #5 under goal #4 [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (4, 1, '24 endpoints across 5 routers', 'initiative', 'completed', 1,
     'CRUD for tasks/projects/objectives + ancillaries (comments, log, tags, links, commits, relations).',
     now() - interval '150 days', now() - interval '130 days');
-- Campaign #6 under initiative #5 [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (5, 1, 'Property-based test sweep for transitions', 'campaign', 'completed', 1,
     '100 generated transition tests for the status matrix.',
     now() - interval '145 days', now() - interval '135 days');
-- Initiative #7 under goal #4 [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (4, 1, 'Publish OpenAPI at /docs', 'initiative', 'completed', 2,
     'FastAPI auto-doc, navigation link, no auth wall.',
     now() - interval '142 days', now() - interval '125 days');
-- Milestone #8: R1 Web UI [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (2, 1, 'R1: Web UI feature-complete', 'milestone', 'active', 2,
     'Dashboard, kanban, task detail, objectives tree, API keys, docs.',
     now() - interval '120 days', now() - interval '1 day');
-- Goal #9 under milestone #8 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (8, 1, 'Task CRUD from the browser', 'goal', 'active', 1,
     'Create, edit, transition, comment, tag — all without curl.',
     now() - interval '90 days', now() - interval '2 hours');
-- Initiative #10 under goal #9 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (9, 1, 'New task form + redirect to detail', 'initiative', 'active', 1,
     '/ui/tasks/new with project/objective/assignee/priority selectors.',
     now() - interval '15 days', now() - interval '1 hour');
-- Initiative #11 under goal #9 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (9, 1, 'Inline edit on task detail', 'initiative', 'active', 2,
     'Replace static <dl> with editable form for 11 fields. htmx swap.',
     now() - interval '14 days', now() - interval '30 minutes');
-- Goal #12 under milestone #8 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (8, 1, 'Objective hierarchy management', 'goal', 'active', 2,
     'Build, edit, and visualise the STR → ROA → MIL → GOAL → INIT → CAMP tree from the UI.',
     now() - interval '12 days', now() - interval '15 minutes');
-- Initiative #13 under goal #12 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (12, 1, 'Tree view with recursive progress rollup', 'initiative', 'active', 1,
     'Recursive CTE for descendant totals.',
     now() - interval '10 days', now() - interval '4 days');
-- Campaign #14 under initiative #13 [draft]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (13, 1, 'Inline create-from-tree (parent_id pre-filled)', 'campaign', 'draft', 1,
     'Click "+ Add child" on any node, form opens with parent_id locked.',
     now() - interval '5 days', now() - interval '5 days');
-- Goal #15 under milestone #8 [completed]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (8, 1, 'Pipeline visualisation (kanban)', 'goal', 'completed', 3,
     'Six pipeline columns + paused / terminal off to the side.',
     now() - interval '80 days', now() - interval '30 days');
-- Milestone #16: R2 MCP/CLI [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (2, 1, 'R2: MCP / CLI parity', 'milestone', 'active', 3,
     'Every REST action available as MCP tool and CLI command.',
     now() - interval '80 days', now() - interval '6 days');
-- Goal #17 under milestone #16 [active]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (16, 1, '24 MCP tools across 5 namespaces', 'goal', 'active', 1,
     'task_*, objective_*, project_*, log_*, pipeline_*.',
     now() - interval '60 days', now() - interval '5 days');
-- Milestone #18: R3 Release gates [draft]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (2, 1, 'R3: Release gates', 'milestone', 'draft', 4,
     'Integration tests, perf budget, docs review before v1.0.0.',
     now() - interval '7 days', now() - interval '7 days');
-- Goal #19 under milestone #18 [draft]
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (18, 1, '100% integration test coverage', 'goal', 'draft', 1,
     'Every endpoint hit by at least one e2e test.',
     now() - interval '7 days', now() - interval '7 days');

-- Project 2 (client-portal) — secondary chain
-- Strategy #20
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (NULL, 2, 'Customer-facing portal for transparency', 'strategy', 'active', 1,
     'Let customers track their deliverables without bothering the team.',
     now() - interval '85 days', now() - interval '10 days');
-- Roadmap #21
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (20, 2, 'v0.1 — read-only customer view', 'roadmap', 'active', 1,
     'Minimum slice: login + project status. No write actions.',
     now() - interval '80 days', now() - interval '8 days');
-- Milestone #22
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (21, 2, 'Customer auth via Okta SSO', 'milestone', 'paused', 1,
     'Blocked on IT delivering SAML metadata.',
     now() - interval '70 days', now() - interval '15 days');
-- Goal #23 under milestone #22
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (22, 2, 'Federated login wired to Okta tenant', 'goal', 'paused', 1,
     'SAML, JIT user provisioning, role mapping.',
     now() - interval '68 days', now() - interval '15 days');

-- Project 3 (mobile-app) — minimal draft chain
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (NULL, 3, 'iOS / Android companion', 'strategy', 'draft', 1,
     'Read tasks on the go. Phase 1 read-only, phase 2 commit comments.',
     now() - interval '25 days', now() - interval '25 days');
-- Roadmap #25
INSERT INTO objectives (parent_id, project_id, title, type, status, sort_order, description, created_at, updated_at) VALUES
    (24, 3, 'v0.1 — read-only viewer', 'roadmap', 'draft', 1,
     'List your assigned tasks, see status changes via push.',
     now() - interval '25 days', now() - interval '25 days');

-- =========================================================================
-- 5. tasks — attached at multiple objective levels
-- =========================================================================
-- TASKS UNDER planner-core (project_id=1)

-- T1 under Campaign #6 (deepest level: campaign) — DONE
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Generate 100 transition test cases',
        'Hypothesis-driven test generator across all status pairs.',
        1, 6, 'bob', 'alice',
        'medium', 'done',
        now() - interval '145 days', now() - interval '136 days', now() - interval '144 days',
        now() - interval '143 days', now() - interval '142 days', now() - interval '136 days',
        'moderate', 'tests/property/test_transitions.py', '5 days');

-- T2 under Campaign #6 — DONE
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Wire transitions into Pipeline class',
        'Centralise status-change logic with one matrix.',
        1, 6, 'claude', 'alice',
        'high', 'done',
        now() - interval '142 days', now() - interval '138 days', now() - interval '141 days',
        now() - interval '141 days', now() - interval '140 days', now() - interval '138 days',
        'moderate', 'planner/pipeline.py', '3 days');

-- T3 under Initiative #5 (one level up) — DONE
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Refactor ancillary routes into single router',
        'Comments/log/tags/links share request prefix; collapse into one file.',
        1, 5, 'bob', 'claude',
        'low', 'done',
        now() - interval '135 days', now() - interval '132 days', now() - interval '134 days',
        now() - interval '134 days', now() - interval '133 days', now() - interval '132 days',
        'simple', 'planner/routers/ancillary.py', '1 day');

-- T4 under Initiative #7 — DONE
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Publish OpenAPI spec at /docs',
        'Auto-generated swagger UI, reachable without auth.',
        1, 7, 'bob', 'alice',
        'medium', 'done',
        now() - interval '140 days', now() - interval '125 days', now() - interval '139 days',
        now() - interval '138 days', now() - interval '138 days', now() - interval '125 days',
        'simple', 'planner/app.py route + nav link', '2 hours');

-- T5 under Goal #9 — DONE (CRUD basics)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Kanban board with htmx auto-refresh',
        'Drag from REST, refresh every 5 s when tab visible.',
        1, 9, 'bob', 'alice',
        'high', 'done',
        now() - interval '60 days', now() - interval '35 days', now() - interval '58 days',
        now() - interval '55 days', now() - interval '54 days', now() - interval '35 days',
        'complex', 'planner/ui/templates/board.html', '2 weeks');

-- T6 under Initiative #10 — DONE (just shipped)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Build /ui/tasks/new form',
        'Project + objective + assignee selectors. Redirect to detail on create.',
        1, 10, 'claude', 'alice',
        'high', 'done',
        now() - interval '5 days', now() - interval '1 hour', now() - interval '4 days',
        now() - interval '4 days', now() - interval '3 days', now() - interval '1 hour',
        'moderate', 'router.py + task_new.html', '1 day');

-- T7 under Initiative #11 — DONE (just shipped)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Inline edit form on task detail (11 fields)',
        'project, objective, assignee, requester, priority, complexity, due, eta, scope, blocked_by, blocked_reason, external_detail.',
        1, 11, 'claude', 'alice',
        'high', 'done',
        now() - interval '3 days', now() - interval '30 minutes', now() - interval '2 days',
        now() - interval '2 days', now() - interval '2 days', now() - interval '30 minutes',
        'moderate', '_partials/task_detail.html + POST /ui/tasks/{id}/edit', '1 day');

-- T8 under Initiative #11 — PENDING_REVIEW
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, complexity_hint, scope, eta, verification_attempts)
VALUES ('Add task title/description editor',
        'Title and description not yet editable from UI — only via REST PATCH.',
        1, 11, 'bob', 'alice',
        'medium', 'pending_review',
        now() - interval '2 days', now() - interval '4 hours', now() - interval '2 days',
        now() - interval '36 hours', now() - interval '30 hours',
        'simple', 'task_detail.html', 'same day', 1);

-- T9 under Initiative #13 — IN_PROGRESS
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, complexity_hint, scope, eta, due_date)
VALUES ('Render objective tree with progress %',
        'Recursive CTE for descendant counts.',
        1, 13, 'alice', 'claude',
        'medium', 'in_progress',
        now() - interval '8 days', now() - interval '6 hours', now() - interval '7 days',
        now() - interval '6 days', now() - interval '4 days',
        'moderate', 'objective_tree.html + _objective_progress_map()', '3 days',
        current_date + 5);

-- T10 under Campaign #14 — OPEN
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, due_date)
VALUES ('"+ Add child" buttons on objective tree',
        'Click adds a new child objective with parent_id pre-filled.',
        1, 14, 'alice', 'alice',
        'medium', 'open',
        now() - interval '4 days', now() - interval '4 days', current_date + 14);

-- T11 under Goal #17 — IN_PROGRESS
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, complexity_hint, scope, eta)
VALUES ('MCP server: implement task_create + task_update',
        'First two of 24 MCP tools wired to TasksRepo.',
        1, 17, 'claude', 'bob',
        'high', 'in_progress',
        now() - interval '12 days', now() - interval '1 day', now() - interval '11 days',
        now() - interval '10 days', now() - interval '8 days',
        'moderate', 'planner/mcp_server.py + mcp_tools/tasks.py', '1 week');

-- T12 under Goal #19 — OPEN
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, due_date)
VALUES ('End-to-end test sweep before v1.0 tag',
        'Hit every endpoint once via httpx + fresh container.',
        1, 19, NULL, 'alice',
        'critical', 'open',
        now() - interval '6 days', now() - interval '6 days', current_date + 30);

-- T13: Free-floating task (no objective) — OPEN
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at)
VALUES ('Draft RFC for v1.1 themes',
        'Plugin system? Audit retention? Webhook outbound?',
        1, NULL, 'alice', 'bob',
        'medium', 'open',
        now() - interval '2 days', now() - interval '2 days');

-- TASKS UNDER client-portal (project_id=2)

-- T14 under Goal #23 — BLOCKED (external_approval)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, previous_status,
                   blocked_reason, external_detail, due_date)
VALUES ('Wire SAML to corporate Okta tenant',
        'IT must publish the SAML metadata URL before this can proceed.',
        2, 23, 'alice', 'bob',
        'high', 'blocked',
        now() - interval '40 days', now() - interval '15 days',
        'in_progress', 'external_approval',
        'Waiting for IT approval request to be approved.',
        current_date + 45);

-- T15 under Goal #23 — REJECTED (cut from scope)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at,
                   complexity_hint, scope)
VALUES ('Embed signup widget directly on marketing site',
        'Rejected after security review — see related ticket.',
        2, 23, 'bob', 'alice',
        'low', 'rejected',
        now() - interval '55 days', now() - interval '50 days', now() - interval '53 days',
        'complex', 'Out of scope for v0.1 portal.');

-- TASKS UNDER mobile-app (project_id=3)

-- T16: no objective yet — OPEN
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at)
VALUES ('Pick mobile stack: RN vs Expo vs Flutter',
        'Spike + decision doc.',
        3, NULL, 'alice', 'alice',
        'medium', 'open',
        now() - interval '20 days', now() - interval '20 days');

-- Plus an INCIDENT + its HOTFIX + its POSTMORTEM (relation graph demo)
-- T17 INCIDENT
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, completed_at)
VALUES ('Pager: /docs returned 500 after deploy',
        'OpenAPI page returned 500 immediately after publish.',
        1, 4, 'claude', 'alice',
        'critical', 'incident',
        now() - interval '124 days', now() - interval '124 days', now() - interval '124 days');

-- T18 HOTFIX (done)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Hotfix: lazy-mount /docs router',
        'Move OpenAPI route registration before middleware.',
        1, 4, 'claude', 'alice',
        'critical', 'done',
        now() - interval '124 days', now() - interval '123 days', now() - interval '124 days',
        now() - interval '124 days', now() - interval '124 days', now() - interval '123 days',
        'simple', 'planner/app.py', '1 hour');

-- T19 POSTMORTEM (done)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, assessed_at, approved_at,
                   execution_started_at, completed_at, complexity_hint, scope, eta)
VALUES ('Post-mortem: /docs 500',
        'Five whys + action items for the OpenAPI 500 incident.',
        1, 4, 'alice', 'bob',
        'medium', 'done',
        now() - interval '122 days', now() - interval '120 days', now() - interval '121 days',
        now() - interval '121 days', now() - interval '121 days', now() - interval '120 days',
        'simple', 'docs/post-mortems/docs-500.md', '3 hours');

-- T20: blocked-by-task demo (depends on T9)
INSERT INTO tasks (title, description, project_id, objective_id, assignee, requester,
                   priority, status, created_at, updated_at, previous_status,
                   blocked_by, blocked_reason)
VALUES ('Add "+ Add child" UI to objective tree',
        'Waits for T9 (tree view) to land first.',
        1, 14, 'alice', 'alice',
        'medium', 'blocked',
        now() - interval '3 days', now() - interval '1 day',
        'open', 9, 'task_dependency');

-- =========================================================================
-- 6. task_comments — a few illustrative ones
-- =========================================================================
INSERT INTO task_comments (task_id, author, content, comment_type, created_at) VALUES
    ( 6, 'alice',  'Approved. Ship it.',                                                              NULL,                now() - interval '4 days'),
    ( 7, 'claude', 'Inline form uses _Unset sentinel pattern so empty-string -> NULL is explicit.',  'review',            now() - interval '20 hours'),
    ( 8, 'bob',    'Review note: title field should validate min length client-side too.',           'review',            now() - interval '5 hours'),
    ( 9, 'claude', 'CTE is correct but I see a subtle bug with cancelled objectives in rollup.',     'assessment',        now() - interval '8 hours'),
    (14, 'alice',  'Escalated to IT director — ticket has been unanswered for 3 weeks.',                       'escalation',        now() - interval '4 days'),
    (17, 'claude', 'Customer-facing impact: /docs page broken for 22 minutes.',                      'incident_followup', now() - interval '124 days'),
    (19, 'alice',  'Action items: (1) deploy smoke test for /docs, (2) staged rollout for app.py.',  'incident_followup', now() - interval '120 days');

-- =========================================================================
-- 7. task_relations (incident graph + parent/child)
-- =========================================================================
INSERT INTO task_relations (task_id, related_id, relation, created_at) VALUES
    ( 9, 20, 'child',      now() - interval '3 days'),
    (20,  9, 'parent',     now() - interval '3 days'),
    (17, 18, 'hotfix',     now() - interval '124 days'),
    (17, 19, 'postmortem', now() - interval '122 days');

-- =========================================================================
-- 8. task_commits
-- =========================================================================
INSERT INTO task_commits (task_id, repo, commit_hash, message, author, content_body, committed_at) VALUES
    ( 4, 'planner-oss', 'a1b2c3d4e5f60718293a4b5c6d7e8f9001122334',
     'feat(api): serve OpenAPI at /docs', 'bob@planner.local',
     'FastAPI auto-generates OpenAPI; expose swagger UI at /docs and link from nav.',
     now() - interval '125 days'),
    ( 6, 'planner-oss', 'b2c3d4e5f6a701829304b5c6d7e8f90011223344',
     'feat(ui): /ui/tasks/new with full create form', 'claude@planner.local',
     'New route + template + button on /ui/tasks. Redirects to detail on success.',
     now() - interval '1 hour'),
    ( 7, 'planner-oss', 'c3d4e5f6a7b801920304b5c6d7e8f9001122334a',
     'feat(ui): inline edit form on task detail', 'claude@planner.local',
     'Replace static <dl> with htmx-posting form, POST /ui/tasks/{id}/edit handler.',
     now() - interval '30 minutes'),
    (18, 'planner-oss', 'd4e5f6a7b8c901a20304b5c6d7e8f90011223344',
     'hotfix(app): lazy-mount /docs router before middleware', 'claude@planner.local',
     'Registration order matters: import-time error in middleware was masking /docs.',
     now() - interval '123 days');

-- =========================================================================
-- 9. task_log — pipeline transitions
-- =========================================================================
INSERT INTO task_log (task_id, action, old_value, new_value, actor, created_at) VALUES
    ( 1, 'created', NULL,                'open',           'alice',  now() - interval '145 days'),
    ( 1, 'status',  'open',              'assessing',      'bob',    now() - interval '144 days'),
    ( 1, 'status',  'assessing',         'awaiting_approval','bob',  now() - interval '143 days'),
    ( 1, 'status',  'awaiting_approval', 'in_progress',    'alice',  now() - interval '142 days'),
    ( 1, 'status',  'in_progress',       'pending_review', 'bob',    now() - interval '137 days'),
    ( 1, 'status',  'pending_review',    'done',           'alice',  now() - interval '136 days'),
    ( 6, 'created', NULL,                'open',           'alice',  now() - interval '5 days'),
    ( 6, 'status',  'open',              'in_progress',    'alice',  now() - interval '3 days'),
    ( 6, 'status',  'in_progress',       'done',           'alice',  now() - interval '1 hour'),
    ( 7, 'created', NULL,                'open',           'alice',  now() - interval '3 days'),
    ( 7, 'status',  'open',              'in_progress',    'alice',  now() - interval '2 days'),
    ( 7, 'status',  'in_progress',       'done',           'alice',  now() - interval '30 minutes'),
    ( 9, 'created', NULL,                'open',           'claude', now() - interval '8 days'),
    ( 9, 'status',  'open',              'in_progress',    'alice',  now() - interval '4 days'),
    (11, 'created', NULL,                'open',           'bob',    now() - interval '12 days'),
    (11, 'status',  'open',              'in_progress',    'claude', now() - interval '8 days'),
    (14, 'created', NULL,                'open',           'bob',    now() - interval '40 days'),
    (14, 'status',  'open',              'in_progress',    'alice',  now() - interval '38 days'),
    (14, 'block',   'in_progress',       'blocked',        'alice',  now() - interval '15 days'),
    (17, 'created', NULL,                'incident',       'claude', now() - interval '124 days'),
    (18, 'created', NULL,                'open',           'alice',  now() - interval '124 days'),
    (18, 'status',  'open',              'done',           'claude', now() - interval '123 days'),
    (19, 'created', NULL,                'open',           'bob',    now() - interval '122 days'),
    (19, 'status',  'open',              'done',           'alice',  now() - interval '120 days'),
    (20, 'created', NULL,                'open',           'alice',  now() - interval '3 days'),
    (20, 'block',   'open',              'blocked',        'alice',  now() - interval '1 day');

-- =========================================================================
-- 10. task_tags
-- =========================================================================
INSERT INTO task_tags (task_id, tag) VALUES
    ( 1, 'tests'), ( 1, 'r0'),
    ( 2, 'tests'), ( 2, 'r0'),
    ( 4, 'docs'),  ( 4, 'r0'),
    ( 5, 'ui'),    ( 5, 'epic:kanban'),
    ( 6, 'ui'),    ( 6, 'sprint-current'),
    ( 7, 'ui'),    ( 7, 'sprint-current'),
    ( 8, 'ui'),    ( 8, 'needs-review'),
    ( 9, 'ui'),    ( 9, 'objective-tree'),
    (10, 'ui'),    (10, 'objective-tree'),
    (11, 'mcp'),
    (12, 'tests'), (12, 'gate'),
    (13, 'rfc'),
    (14, 'auth'),  (14, 'blocked:approval'),
    (17, 'incident'), (17, 'sev:high'),
    (18, 'hotfix'),
    (19, 'postmortem'),
    (20, 'ui'),    (20, 'objective-tree'),    (20, 'blocked:dependency');

-- =========================================================================
-- 11. task_links
-- =========================================================================
INSERT INTO task_links (task_id, kind, url, label, created_at) VALUES
    ( 4, 'pr',       'https://github.com/example/planner-oss/pull/12',  'PR #12',          now() - interval '127 days'),
    ( 4, 'deploy',   'https://example.com/deploys/2025-12-15',           'Deploy log',      now() - interval '125 days'),
    ( 6, 'pr',       'https://github.com/example/planner-oss/pull/302', 'PR #302',         now() - interval '1 hour'),
    ( 7, 'pr',       'https://github.com/example/planner-oss/pull/303', 'PR #303',         now() - interval '30 minutes'),
    ( 9, 'plan',     'https://example.com/plans/objective-tree',         'Plan doc',        now() - interval '7 days'),
    (11, 'document', 'https://example.com/specs/mcp-tools',              'MCP spec',        now() - interval '11 days'),
    (14, 'external', 'https://it.example.com/tickets/1287',              'IT approval request', now() - interval '15 days'),
    (17, 'external', 'https://status.example.com/incidents/2025-12-16',  'Status page',     now() - interval '124 days'),
    (18, 'pr',       'https://github.com/example/planner-oss/pull/45',  'Hotfix PR #45',   now() - interval '123 days'),
    (19, 'document', 'https://example.com/post-mortems/docs-500',        'Post-mortem',     now() - interval '120 days');

-- =========================================================================
-- 12. task_notifications
-- =========================================================================
INSERT INTO task_notifications (task_id, event_type, notify_target, payload, created_at, fired_at) VALUES
    ( 6, 'task.status_changed',  'email:requester',
     '{"from":"in_progress","to":"done","by":"alice"}'::jsonb,
     now() - interval '1 hour', now() - interval '1 hour' + interval '8 seconds'),
    ( 7, 'task.status_changed',  'webhook:slack',
     '{"from":"in_progress","to":"done","by":"alice"}'::jsonb,
     now() - interval '30 minutes', now() - interval '30 minutes' + interval '3 seconds'),
    (14, 'task.blocked',         'email:requester',
     '{"reason":"external_approval","external_detail":"IT approval request"}'::jsonb,
     now() - interval '15 days', now() - interval '15 days' + interval '5 seconds'),
    (17, 'task.incident_opened', 'email:requester',
     '{"severity":"high"}'::jsonb,
     now() - interval '124 days', now() - interval '124 days' + interval '4 seconds'),
    ( 8, 'task.status_changed',  'agent:secretary',
     '{"from":"in_progress","to":"pending_review","by":"bob"}'::jsonb,
     now() - interval '30 hours', NULL),
    (20, 'task.blocked',         'webhook:slack',
     '{"reason":"task_dependency","blocked_by":9}'::jsonb,
     now() - interval '1 day', NULL);

COMMIT;

-- Quick verification
SELECT 'projects' AS t, count(*) FROM projects
UNION ALL SELECT 'objectives',  count(*) FROM objectives
UNION ALL SELECT 'tasks',       count(*) FROM tasks
UNION ALL SELECT 'comments',    count(*) FROM task_comments
UNION ALL SELECT 'relations',   count(*) FROM task_relations
UNION ALL SELECT 'commits',     count(*) FROM task_commits
UNION ALL SELECT 'log',         count(*) FROM task_log
UNION ALL SELECT 'tags',        count(*) FROM task_tags
UNION ALL SELECT 'links',       count(*) FROM task_links
UNION ALL SELECT 'notif',       count(*) FROM task_notifications
UNION ALL SELECT 'actors',      count(*) FROM actors
UNION ALL SELECT 'api_keys',    count(*) FROM api_keys
ORDER BY t;

-- Show the strategy chain for project 1
WITH RECURSIVE tree AS (
    SELECT id, parent_id, title, type, status, 0 AS depth,
           lpad('', 0, ' ') AS indent
      FROM objectives
     WHERE project_id = 1 AND parent_id IS NULL
    UNION ALL
    SELECT o.id, o.parent_id, o.title, o.type, o.status, t.depth + 1,
           lpad('', (t.depth + 1) * 2, ' ')
      FROM objectives o
      JOIN tree t ON o.parent_id = t.id
)
SELECT indent || '#' || id || ' [' || upper(left(type, 3)) || '] '
       || title || '  (' || status || ')' AS hierarchy
  FROM tree
 ORDER BY id;
