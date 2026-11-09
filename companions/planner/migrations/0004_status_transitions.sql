-- 0004_status_transitions.sql
-- DB-level status-transition lock. The PATCH route calls
-- TasksRepo.update (raw CRUD) and bypasses the pipeline.py state machine, so an
-- agent could set a valid-value / wrong-stage status (observed: open ->
-- pending_review). This rejects an invalid transition at the DB for EVERY writer.
--
-- Seeded from pipeline.py TRANSITIONS plus two transitions the running system
-- needs and the design matrix omitted:
--   open -> awaiting_approval  (a complex task assessed at intake goes straight to
--                               plan-approval; matches the planner tool contract)
--   awaiting_approval -> done  (the flow approval-gate approve path in runner.py;
--                               gate tasks are created directly as awaiting_approval)
-- Emergency valve: SET planner.bypass_transitions='on' in a session disables it
-- (the trusted state-machine driver / an admin correction).

CREATE TABLE IF NOT EXISTS status_transitions (
    from_status text NOT NULL,
    to_status   text NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

INSERT INTO status_transitions (from_status, to_status) VALUES
  ('open','assessing'),('open','in_progress'),('open','rejected'),('open','blocked'),('open','awaiting_approval'),
  ('assessing','awaiting_approval'),('assessing','in_progress'),('assessing','blocked'),('assessing','escalated'),('assessing','rejected'),
  ('awaiting_approval','in_progress'),('awaiting_approval','rejected'),('awaiting_approval','blocked'),('awaiting_approval','escalated'),('awaiting_approval','done'),
  ('in_progress','pending_review'),('in_progress','blocked'),('in_progress','escalated'),('in_progress','done'),
  ('pending_review','done'),('pending_review','in_progress'),('pending_review','escalated'),
  ('blocked','open'),('blocked','assessing'),('blocked','awaiting_approval'),('blocked','in_progress'),('blocked','pending_review'),
  ('escalated','open'),('escalated','assessing'),('escalated','in_progress'),('escalated','rejected'),
  ('rejected','open'),('rejected','escalated'),
  ('done','incident'),
  ('incident','in_progress'),('incident','done')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION enforce_status_transition() RETURNS trigger AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    IF coalesce(current_setting('planner.bypass_transitions', true), '') = 'on' THEN
      RETURN NEW;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM status_transitions
                   WHERE from_status = OLD.status AND to_status = NEW.status) THEN
      RAISE EXCEPTION 'invalid status transition % -> % (allowed from %: %)',
        OLD.status, NEW.status, OLD.status,
        coalesce((SELECT string_agg(to_status, ', ' ORDER BY to_status)
                    FROM status_transitions WHERE from_status = OLD.status), '(none)')
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_status_transition ON tasks;
CREATE TRIGGER trg_enforce_status_transition
  BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION enforce_status_transition();
