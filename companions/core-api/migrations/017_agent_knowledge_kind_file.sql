-- 017_agent_knowledge_kind_file.sql
-- Allow the 'file' kind in agent_knowledge (uploaded files: PDF/DOCX/XLSX/PPTX/HTML
-- converted to markdown via the host markitdown and snapshotted into the agent's
-- mesh workspace). file-upload iteration.
--
-- Idempotent: safe to re-run.

ALTER TABLE sn_config.agent_knowledge DROP CONSTRAINT IF EXISTS agent_knowledge_kind_check;
ALTER TABLE sn_config.agent_knowledge ADD CONSTRAINT agent_knowledge_kind_check
    CHECK (kind = ANY (ARRAY['text','doci','url','gdrive_file','file']));
