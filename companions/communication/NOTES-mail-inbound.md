# Mail inbound -- per-agent mailboxes + registry poller

How incoming email becomes rows in the core registry (`sn_core.messages`),
and the two deployment shapes proven on real nodes.

## The model

- Agents have per-agent mailboxes (`<agent-slug>@<domain>`,
  ...). The mapping lives in `sn_config.agent_channels` (channel `email`,
  `instance_ref` = the address).
- `poll_mailboxes.py` (driven by the `smart-node-communication-email`
  timer) reads that list, polls each mailbox over IMAP, and records every
  UNSEEN message as one immutable inbound row in `sn_core.messages`,
  **agent-agnostic** (`recipient` = the To address; no agent binding, no
  brin dispatch). Dedup is the `(channel_type, external_id)` unique index.
- Per-mailbox IMAP/SMTP passwords come from a generate-once creds file
  (`AGENT_ACCOUNTS_FILE`, default `/opt/smart-node/mail/agent-accounts.env`,
  keyed `PW_<addr>` with `@`/`.` -> `_`). A mailbox with no entry is
  skipped (logged), never fatal.

## Shape A -- self-contained (the node's own Maddy) -- REMOVED 2026-08-02

The `mail` companion (`companions/mail.sh`) ran Maddy on the node and is gone.
It was never wired into the orchestrator, but the copy it had already installed on reference-node outlived its purpose: seven mailbox names identical to the
real ones on the mail server, a second set of passwords, a DKIM key for
the node's own domain under a selector that was never published, and a queue that
would have delivered straight to MX. It served no mail after 2026-06-26 and
held one test message, yet anything pointed at it would have authenticated,
read an empty inbox, and reported success.

A node does not host mail. It reads and sends through the one mail service, by
its protocol names, authenticating as the mailbox it claims to be. That is
Shape B, and it is now the only shape.

## Shape B -- public edge via a host DMS (proven on reference-node)

When the node sits behind a host that already owns public :25 (a
docker-mailserver / DMS serving other domains), public mail for the
node's domain is received by that DMS and the node polls it. No change to
the DMS Postfix relay -- the agent mailboxes are real DMS accounts.

Recipe (a node behind an inbound mail relay, domain example.com):
1. DMS: `setup email add secretary@<domain> <pw>` per agent box (this
   makes `<domain>` a received virtual domain). Store the passwords in
   the node's creds file (here `/opt/smart-node/mail/dms-accounts.env`).
2. DMS DKIM for the domain: `setup config dkim keytype rsa keysize 2048
   selector mail domain <domain>`, then add a domain block to
   `rspamd/override.d/dkim_signing.conf` (selector `mail`, the generated
   key path) and restart rspamd. Needed so agent *replies* are signed.
3. DNS: `MX <domain> -> <dms-fqdn>`; `TXT <domain> "v=spf1 mx ~all"`;
   `TXT mail._domainkey.<domain>` (the DKIM public record); `TXT
   _dmarc.<domain> "v=DMARC1; p=none; rua=..."`. Point MX at the DMS's
   existing FQDN to reuse its valid PTR + cert.
4. Node `.env`: `IMAP_HOST`/`SMTP_HOST` = the DMS (reachable host IP),
   `EMAIL_TLS_VERIFY=0` (cert CN is the DMS FQDN, reached by IP),
   `AGENT_ACCOUNTS_FILE` = the DMS creds file.

Shape B trade-off: the mailboxes live on the shared host DMS, not on the
node's Maddy (which is then idle for inbound). Chosen on reference-node to get a
public MX without editing the prod DMS relay config.

## Replies

Either shape: an agent authenticates submission as itself
(`secretary@<domain>`) and sends; `adapters.email.adapter.deliver()`
records the outbound row and threads via `In-Reply-To`/`References`
(the inbound row stores `thread_id` + those headers in `metadata`).
SPF/DKIM/DMARC (Shape B step 2-3) make replies deliverable.
