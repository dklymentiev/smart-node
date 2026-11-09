-- Reference migration for an agent's model is an enabled chat row of
-- sn_config.models, checked by a trigger instead of an enumerating CHECK.
--
-- Production schema is applied by bootstrap/B02-sn-core-schema.sql, which carries
-- the same function and the guarded drop/create. companions/core-api/migrations is
-- retained as the component-level schema history; core-api.sh does not run it.

CREATE OR REPLACE FUNCTION sn_config.agents_model_in_catalog() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.model = 'none' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.model IS NOT DISTINCT FROM OLD.model THEN
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM sn_config.models m
                WHERE m.model = NEW.model AND m.enabled AND m.modality = 'chat') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'model % is not an enabled chat model in sn_config.models', quote_literal(NEW.model)
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'agents_model_in_catalog',
              HINT = 'Add or enable the row in sn_config.models; the panel lists exactly those rows.';
END
$fn$;

ALTER TABLE sn_config.agents DROP CONSTRAINT IF EXISTS agents_model_check;
DROP TRIGGER IF EXISTS agents_model_in_catalog ON sn_config.agents;
CREATE TRIGGER agents_model_in_catalog
    BEFORE INSERT OR UPDATE OF model ON sn_config.agents
    FOR EACH ROW EXECUTE FUNCTION sn_config.agents_model_in_catalog();
