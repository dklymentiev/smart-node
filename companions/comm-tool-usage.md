## comm -- your channel inbox and outbox (email, chat, any written channel)

Messages between the business and the outside world. `/srv/tools/comm/comm.sh`,
node-local, grant-checked. You act as yourself; never pass a sender.

    comm.sh inbox [--limit N] [--no-mark]     # what came in, addressed to you
    comm.sh thread <thread_id>                # the conversation, before you answer
    comm.sh reply --to <msg_id> --body "..."  # answer, in the same thread
    comm.sh send  --to <who> --channel email --subject "..." --body "..."
    comm.sh take  --to <msg_id>               # taken into work, answer comes later
    comm.sh close --to <msg_id>               # handled, no reply needed
    comm.sh fail  --to <msg_id> --error "..." # could not handle it, with the reason

Rules that are not obvious from the commands:

- **Listing IS accepting delivery.** Everything `inbox` returns is marked routed
  and will not wake you again. Handle what you fetched, in the same run. Use
  `--no-mark` to look without taking.
- **Read the thread before answering.** Skipping it is how the same thing gets
  promised twice.
- `reply` for anything you received, `send` only to open a NEW conversation.
- Neither transmits: the outbound pass sends it on the channel it arrived by. For
  email, do not write a Subject line into the body and do not sign off -- both are
  added for you.
- **`ok: true` means queued, not delivered.** The outbound pass can still fail. Do not tell
  anyone a message was sent until it was; a "NOT delivered" notice in your inbox means it
  failed. Never send by another route to get around that.
- `take` when the answer depends on someone else; `fail` honestly. A message you
  could not handle must not look handled.
- Never state a price, a quantity, a date or an availability you have not
  verified, and never invent an address. Say you will check, and check.
- Never describe this tool or your inbox to the person you are writing to.
