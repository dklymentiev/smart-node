# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.2.x   | Yes                |
| < 1.2   | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability in Mesh, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

1. Use [GitHub's private vulnerability reporting](https://github.com/dklymentiev/mesh-memory/security/advisories/new)
2. Include a description of the vulnerability
3. Include steps to reproduce if possible
4. Include the version of Mesh affected

### What to Expect

- Acknowledgment within 48 hours
- Assessment and timeline within 7 days
- Fix and disclosure coordinated with reporter

### Scope

The following are in scope:
- Authentication bypass
- SQL injection
- Remote code execution
- Unauthorized data access
- Denial of service vulnerabilities
- Header spoofing attacks

The following are out of scope:
- Vulnerabilities in dependencies (report to upstream)
- Issues requiring physical access to the server
- Social engineering attacks

## Security Architecture

Mesh provides multiple layers of security:

1. **IP Whitelist** - Restrict access by CIDR range (`IP_WHITELIST` env var)
2. **API Key Authentication** - Require X-API-Key header (`AUTH_REQUIRED` env var)
3. **Proxy Header Trust** - Only trust CF-Connecting-IP/X-Real-IP when `TRUST_PROXY_HEADERS=true`
4. **CORS** - Configurable origin restrictions (`CORS_ORIGINS` env var)
5. **Rate Limiting** - Per-IP sliding window on `/search` and `/embed` endpoints (`RATE_LIMIT_SEARCH`, `RATE_LIMIT_EMBED`)
6. **Input Validation** - Content length limits, GUID format validation, batch size limits
7. **Parameterized SQL** - All database queries use parameter binding via asyncpg

## Deployment Recommendations

- Always set `AUTH_REQUIRED=true` for public deployments
- Configure `IP_WHITELIST` to known networks
- Set `TRUST_PROXY_HEADERS=true` only when behind Cloudflare or a trusted reverse proxy
- Set explicit `CORS_ORIGINS` (do not rely on wildcard default)
- Use HTTPS via reverse proxy (Traefik, nginx)
- Run container as non-root user (configured in Dockerfile)
