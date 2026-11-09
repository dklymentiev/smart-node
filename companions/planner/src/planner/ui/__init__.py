"""Web UI for Planner.

Server-rendered Jinja2 templates with HTMX for interactivity and
Tailwind CSS (CDN) for styling. No build step, no SPA framework.

The UI calls into the same repos as the REST API (and shares the same
DB pool), so the four interfaces stay parity-tested.
"""
