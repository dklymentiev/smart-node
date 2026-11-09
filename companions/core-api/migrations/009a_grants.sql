-- Stage 2a addendum to Migration 009 (2026-05-24)
-- 
-- Default privileges in sn_config schema do NOT auto-cover newly created tables
-- (unlike sn_core which has default_privileges configured). So new sn_config tables
-- from Migration 009 need explicit GRANT statements for smartnode role.
-- 
-- Idempotent — safe to re-run.
GRANT SELECT, INSERT, UPDATE, DELETE ON sn_config.model_pricing TO smartnode;
GRANT USAGE ON SEQUENCE sn_config.model_pricing_id_seq TO smartnode;
GRANT SELECT, INSERT, UPDATE, DELETE ON sn_config.budgets TO smartnode;
GRANT USAGE ON SEQUENCE sn_config.budgets_id_seq TO smartnode;
GRANT SELECT, INSERT, UPDATE, DELETE ON sn_config.cli_providers TO smartnode;
GRANT USAGE ON SEQUENCE sn_config.cli_providers_id_seq TO smartnode;
GRANT SELECT, INSERT, UPDATE, DELETE ON sn_config.session_retention TO smartnode;
GRANT USAGE ON SEQUENCE sn_config.session_retention_id_seq TO smartnode;
