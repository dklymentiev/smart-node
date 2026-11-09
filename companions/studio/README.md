# Smart Node Studio

New end-user interface for Smart Node, the self-hosted AI platform for small business.

Concept: a **Spatial Digital Office** — a manager's desk staffed by virtual employees (AI agents),
not a one-on-one chatbot. Core UX principles: Human-in-the-Loop (nothing leaves to a client without
owner approval) and Progressive Disclosure (engineer settings hidden behind Advanced mode).

Stack: **vanilla HTML/CSS/JS** on the canonical UI kit (`--sn-*` tokens). No build step, no framework.

Status: **Phase 0 (local) — Express server + empty authenticated shell + Log out.**
Not deployed yet; public demo (Cloudflare) not wired.

## Run (local)

    PORT=8050 HOST=127.0.0.1 AUTHUM_AUTH_URL=https://auth.example.com npm start

Auth is enforced at the edge (Traefik + Authum forwardAuth injects `Remote-User`).
`/api/me` reports the logged-in user + the Authum logout URL. Section backends
(memory/docs/planner/workflows) are added in later phases as BFF proxies.
