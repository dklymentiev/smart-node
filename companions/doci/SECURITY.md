# Security Policy

## Supported Versions

DOCI is pre-1.0 (current: 0.1.x). Until a 1.0 release ships, only the
most recent tagged release receives security fixes; older 0.x releases
do not. A formal support matrix appears here once 1.x lands.

| Version | Supported            |
| ------- | -------------------- |
| 0.1.x   | :white_check_mark:   |
| < 0.1   | :x:                  |

## Reporting a Vulnerability

If you discover a security vulnerability in DOCI, please report it responsibly:

1. **Do not** create a public GitHub issue
2. Email: security@klymentiev.com
3. Or use GitHub's private vulnerability reporting feature on this repository
4. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a detailed response within 7 days.

## Security Measures

DOCI implements the following security measures:

- **Authentication**: External authentication via Traefik ForwardAuth
- **CSRF Protection**: Token-based protection for state-changing requests
- **SQL Injection**: All queries use parameterized statements
- **Path Traversal**: Input sanitization and realpath validation
- **Session Security**: Secure cookies with httponly, secure, samesite flags
- **Secrets**: All credentials via environment variables

## Best Practices for Deployment

1. Always use HTTPS in production
2. Set strong `DB_PASS` and `DOCI_API_KEY_HASH`
3. Keep `DOCI_ENV=production` and `DOCI_DEV_AUTO_AUTH=false` (defaults)
   — the app refuses to start serving requests if `DOCI_DEV_AUTO_AUTH=true`
   without `DOCI_ENV=development`. Keep `DOCI_LOG_LEVEL=WARN` (the
   production default) unless actively debugging; security-relevant
   actions log regardless of level.
4. Regularly update dependencies
5. Monitor logs for suspicious activity
