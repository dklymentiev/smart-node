-- 031: backfill sn_core.desks for orphaned desk conversations.
--
-- Root cause: before the Studio UI called POST /api/desks, the browser generated
-- a UUID client-side and used it as the desk id. brin accepted the id and wrote
-- messages under it, but no row was ever created in sn_core.desks. The listing
-- endpoint reads from desks, so only the conversations that went through the
-- newer UI path were visible. All message data is intact; only the index is missing.
--
-- This migration creates one desks row per orphaned desk UUID, derived from
-- sn_core.ai_sessions. Owner is the super-admin (single-tenant reality). Agent
-- is taken from the most recent session that served the desk. Title is the first
-- user message, truncated to 200 chars; falls back to a date string.
--
-- Safe to run multiple times: ON CONFLICT (id) DO NOTHING skips existing rows.

DO $$
DECLARE
    v_owner_id  integer;
    v_tenant_id integer;
    v_inserted  integer;
BEGIN
    -- Resolve the node owner (super-admin). Bail if none found.
    SELECT id, COALESCE(tenant_id, 1)
      INTO v_owner_id, v_tenant_id
      FROM sn_config.users
     WHERE role = 'super-admin'
       AND active
       AND archived_at IS NULL
     ORDER BY id
     LIMIT 1;

    IF v_owner_id IS NULL THEN
        RAISE NOTICE '031_desk_backfill: no super-admin found; skipping.';
        RETURN;
    END IF;

    -- Insert one row per orphaned desk UUID.
    -- Source: ai_sessions whose metadata.conversation starts with 'desk:'.
    -- Excludes desk ids that already have a desks row.
    WITH orphans AS (
        SELECT
            replace(s.metadata->>'conversation', 'desk:', '')::uuid  AS desk_id,
            s.tenant_id,
            min(s.created_at)          AS first_seen,
            max(s.last_activity_at)    AS last_seen
        FROM sn_core.ai_sessions s
        WHERE s.metadata->>'conversation' LIKE 'desk:%'
          AND length(replace(s.metadata->>'conversation', 'desk:', '')) = 36
          -- only rows where no desks entry exists yet
          AND NOT EXISTS (
              SELECT 1 FROM sn_core.desks d
               WHERE d.id::text = replace(s.metadata->>'conversation', 'desk:', '')
          )
        GROUP BY replace(s.metadata->>'conversation', 'desk:', ''), s.tenant_id
    ),
    enriched AS (
        SELECT
            o.desk_id,
            COALESCE(o.tenant_id, v_tenant_id) AS tenant_id,
            o.first_seen,
            o.last_seen,
            -- agent: slug from the most recent session for this desk
            (
                SELECT a.slug
                FROM sn_core.ai_sessions ss
                JOIN sn_config.agents a ON a.id = ss.agent_id
                WHERE ss.metadata->>'conversation' = 'desk:' || o.desk_id::text
                ORDER BY ss.created_at DESC
                LIMIT 1
            ) AS agent_slug,
            -- title: first user message on this desk (any session), up to 200 chars
            (
                SELECT left(m.content, 200)
                FROM sn_core.ai_session_messages m
                JOIN sn_core.ai_sessions ss ON ss.id = m.session_id
                WHERE ss.metadata->>'conversation' = 'desk:' || o.desk_id::text
                  AND m.role = 'user'
                ORDER BY m.created_at ASC, m.seq ASC
                LIMIT 1
            ) AS first_msg
        FROM orphans o
    )
    INSERT INTO sn_core.desks
        (id, tenant_id, owner_user_id, title, metadata, created_at, updated_at)
    SELECT
        e.desk_id,
        e.tenant_id,
        v_owner_id,
        COALESCE(
            nullif(trim(e.first_msg), ''),
            'Chat from ' || to_char(e.first_seen AT TIME ZONE 'UTC', 'YYYY-MM-DD')
        ),
        CASE WHEN e.agent_slug IS NOT NULL
             THEN jsonb_build_object('agent', e.agent_slug)
             ELSE '{}'::jsonb
        END,
        e.first_seen,
        COALESCE(e.last_seen, e.first_seen)
    FROM enriched e
    ON CONFLICT (id) DO NOTHING;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RAISE NOTICE '031_desk_backfill: % desk(s) backfilled.', v_inserted;
END $$;
