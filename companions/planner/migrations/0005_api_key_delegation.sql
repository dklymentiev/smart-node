-- Planner: delegated attribution for a trusted key
-- Migration 0005
--
-- A key marked can_delegate may name the caller it is acting for, through the
-- proxy-user header, instead of authoring everything as its own actor. This
-- exists for a front door that has already resolved somebody else's identity
-- and needs to present BOTH a credential and the real author in one request.
--
-- Neither mode could do that before. `apikey` closes the door but authors every
-- call as the key's owner, so a shared front door collapses every caller into
-- one name. `proxy` keeps the real name but trusts the header from anyone who
-- can reach the socket. The flag separates the two questions: the KEY answers
-- "may this caller in at all", the header answers "on whose behalf", and the
-- second is read only when the first says the caller is trusted to be asked.
--
-- Defaults to false, and that default is the point: a key that was never told
-- it may speak for others must not begin doing so because a header appeared.

BEGIN;

ALTER TABLE api_keys
    ADD COLUMN can_delegate BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN api_keys.can_delegate IS
    'When true, a request bearing this key may name its actor through the proxy-user header (PLANNER_PROXY_USER_HEADER, default Remote-User) and the entry is authored by that actor. When false the header is ignored entirely and the key''s own actor_id authors everything. Grant only to a component that authenticates its own callers.';

COMMIT;
