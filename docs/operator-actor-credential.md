# Operator actor credential

A remote operator is an actor `component:<username>` with role `admin`. Its
bearer token proves that actor at core-api's TCP door. The shared host login
`admin` is not an operator credential.

An operator is node configuration. A fresh install has none: the product seed
creates no operator actor, no operator account and no token. The owner
registers each operator on the node it works on.

## Register an operator and get its token

1. The owner signs in through the panel.
2. In that signed-in browser session, send

   ```
   POST /api/operators
   Content-Type: application/json

   {"username": "<username>", "display_name": "<label shown in the panel>"}
   ```

   `username` starts with a lowercase letter and contains only lowercase
   letters, digits, `-` or `_`, 32 characters at most. `display_name` is
   optional and defaults to `Operator`.

   The owner can send this from the browser's developer tools on the panel
   origin; Authum and Traefik attach the session proof to the same-origin
   `/api` request.
3. The response is `201` and shows `token` once:

   ```
   {"actor_id": "component:<username>", "username": "<username>",
    "role": "admin", "token": "<token>", "note": "Token shown once; rotate if lost."}
   ```

   Transfer the token to the operator's approved private credential store. Do
   not put it in a command, event, issue, transcript, or repository file. The
   database stores only its SHA-256 digest.
4. From the operator's environment, call an API route the `admin` role may
   use, for example `GET /api/agents`, with `Authorization: Bearer <token>`.
   Confirm a successful response and inspect the resulting
   `auth.permission_granted` event: actor `component:<username>`, role
   `admin`. The bearer resolver marks the principal strength `proved`. Check
   this before deploying a release that refuses asserted human identities
   over TCP: the refusal has no switch.

The one request creates the actor, an active account with the same username,
the `admin` role grant and the first bearer credential, in one transaction. A
refused request leaves none of them behind.

## Who may register an operator

The route requires both the `roles.grant` permission, which only the
super-admin role holds, and a signed-in session.

- A bearer token cannot register an operator, not even a token that belongs
  to the owner. An operator therefore cannot mint another operator.
- A direct TCP request with only `Remote-User: admin` is refused as well.

## Refusals

| Status | Meaning |
|--------|---------|
| 403 | The caller lacks `roles.grant`, or is not a signed-in session. |
| 409 | The username already belongs to an account or an agent, or the actor `component:<username>` already exists. Choose another username. |
| 422 | The username does not match the pattern above. |
| 503 | The node cannot verify sessions because the proxy proof is not configured. |

A node updated from a release that created its own operator rows may already
hold an actor or an account under that name. Registration does not adopt
them: it answers `409`, and the owner registers the operator under a new
username.

## Rotation and revocation

- `POST /api/actors/component:<username>/credentials/bearer_token/rotate`
  returns a new token once and invalidates the old token in the same database
  update. It also requires the owner's signed-in super-admin session.
- `DELETE /api/actors/component:<username>/credentials/bearer_token` revokes
  the token. After revocation, a bearer request is refused.
- After a revocation, `POST /api/actors/component:<username>/credentials` with
  `{"cred_type":"bearer_token"}` issues a new token to the same operator.
- A lost token is rotated by the owner. The digest cannot recover plaintext.

## Unix socket

To let a dedicated operator OS account use the Unix socket, the owner adds an
`os_user` credential to the registered operator through
`POST /api/actors/component:<username>/credentials` with
`{"cred_type":"os_user","cred_value":"<os account>"}`. The socket resolver
checks registered `os_user` credentials after agent OS accounts; an account
that belongs to an agent still resolves as that agent. A shared OS account is
not registered as an operator's `os_user` credential.
