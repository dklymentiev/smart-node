"""Give the node's owner the channel rows that prove they are a person.

`sn_config.user_channels` is empty on every node born from canon. The table is
created by the baked schema; its only seed lives in migration 016, and core-api.sh
says plainly that the vendored migrations are reference only and nothing applies
them. Even had it run, it makes an EMAIL row -- nothing has ever made a VOICE one.

Two things break quietly without a row here, and both were live for months:

  * A call's transcript is not stored. /api/messages/record-voice looks up a voice
    channel for the caller's identity, finds none, and answers HTTP 200 with
    {"ok": false, "recorded": 0} -- so the voice side, which logs the status code
    and not the body, reports success while writing nothing. Nobody could read
    back what an assistant had said, which is half of any honest acceptance test.

  * Owner notifications go nowhere. /api/notify resolves an assignee to their
    enabled channels and returns {"sent": []} when there are none -- and the
    endpoint documents "no rows" as "this is an agent, skip". A human owner with
    no rows is therefore indistinguishable from an agent.

Idempotent, and never overwrites: an operator who sets a real address in the panel
keeps it across restarts.
"""

import logging

import psycopg2


log = logging.getLogger("seed-user-channels")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> None:
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor() as cur:
            # The node's own super-admin: the identity a call authenticates as.
            cur.execute(
                "SELECT id, tenant_id, email FROM sn_config.users "
                "WHERE active AND archived_at IS NULL AND role <> 'agent' "
                "ORDER BY id LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                log.info("no human user yet -- nothing to give a channel to")
                return
            uid, tenant, email = row
            # voice: address is the identity, prefixed. record-voice matches on the
            # user's email OR handle OR this address, so any of the three resolves.
            cur.execute(
                "INSERT INTO sn_config.user_channels "
                "  (tenant_id, user_id, channel_slug, address, enabled) "
                "SELECT %s, %s, 'voice', %s, true "
                "WHERE NOT EXISTS (SELECT 1 FROM sn_config.user_channels "
                "                  WHERE user_id = %s AND channel_slug = 'voice')",
                (tenant, uid, "voice:%s" % (email or uid), uid),
            )
            log.info("voice channel for user %s: %s", email or uid,
                     "created" if cur.rowcount else "already there")
            # web-chat: the owner is the person at the browser. record-webchat
            # accepts identity='owner' as a special token; this row gives it a
            # stable address so cross-channel-history can find those messages.
            # Address is 'web:owner' -- same as the synthetic fallback in
            # record-webchat, so existing rows written before this seeding are
            # also matched once the channel is registered.
            cur.execute(
                "INSERT INTO sn_config.user_channels "
                "  (tenant_id, user_id, channel_slug, address, enabled) "
                "SELECT %s, %s, 'web-chat', 'web:owner', true "
                "WHERE NOT EXISTS (SELECT 1 FROM sn_config.user_channels "
                "                  WHERE user_id = %s AND channel_slug = 'web-chat')",
                (tenant, uid, uid),
            )
            log.info("web-chat channel for user %s: %s", email or uid,
                     "created" if cur.rowcount else "already there")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
