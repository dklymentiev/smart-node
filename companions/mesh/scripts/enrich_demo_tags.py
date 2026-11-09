#!/usr/bin/env python3
"""
Enrich demo documents with category/topic/project tags.
Reads documents, classifies by content keywords, adds tags via PATCH.

Usage:
    python scripts/enrich_demo_tags.py --api http://localhost:8001
    python scripts/enrich_demo_tags.py --api https://mesh-demo.example.com
"""

import argparse
import re
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Classification rules: keyword patterns -> tags
# ---------------------------------------------------------------------------

CATEGORY_RULES = [
    # (category, subcategories_with_patterns)
    ("category:software-development", [
        ("topic:api-design", r"API\b|REST|GraphQL|endpoint|OpenAPI|gRPC"),
        ("topic:code-review", r"code review|PR #|pull request|reviewed|feedback"),
        ("topic:debugging", r"debug|memory leak|root cause|bug fix|stack trace|traceback"),
        ("topic:refactoring", r"refactor|optimization|optimize|migrat|rewrite"),
        ("topic:testing", r"test|TDD|unit test|integration test|coverage|pytest|jest"),
        ("topic:ci-cd", r"CI/CD|pipeline|GitHub Actions|deploy|continuous integration"),
        ("topic:typescript", r"TypeScript|tsconfig|\.ts\b|type annotation"),
        ("topic:python", r"Python|FastAPI|Django|Flask|pip|asyncio|pydantic"),
        ("topic:javascript", r"JavaScript|Node\.js|npm|React|Vue|webpack"),
        ("topic:rust", r"\bRust\b|cargo|ownership|borrowing|lifetime"),
        ("topic:git", r"\bgit\b|commit|branch|merge|rebase|cherry-pick|worktree"),
        ("topic:websocket", r"WebSocket|real-time|SSE|server-sent|socket"),
        ("topic:database", r"SQL|PostgreSQL|database|query|index|migration|schema"),
        ("topic:redis", r"Redis|cache|caching|rate.?limit"),
        ("topic:authentication", r"auth|JWT|token|login|SSO|OAuth|session"),
        ("topic:error-handling", r"error handling|exception|try.?catch|retry|fallback"),
        ("topic:microservices", r"microservice|service.?mesh|gRPC|message queue"),
    ]),
    ("category:infrastructure", [
        ("topic:docker", r"Docker|container|Dockerfile|docker.?compose|image"),
        ("topic:kubernetes", r"Kubernetes|k8s|kubectl|pod|deployment|helm"),
        ("topic:monitoring", r"monitor|Prometheus|Grafana|alert|metric|observ"),
        ("topic:backup", r"backup|restore|recovery|disaster|retention|pg_dump"),
        ("topic:networking", r"DNS|CDN|Cloudflare|proxy|Nginx|Traefik|SSL|TLS|HTTP"),
        ("topic:linux", r"Linux|Ubuntu|bash|shell|systemd|cron|server"),
        ("topic:logging", r"log|Loki|ELK|Elasticsearch|logql|aggregat"),
        ("topic:security", r"security|firewall|fail2ban|SSH|hardening|encrypt"),
        ("topic:automation", r"Ansible|provision|automat|playbook|terraform"),
    ]),
    ("category:ai-ml", [
        ("topic:llm", r"LLM|language model|GPT|Claude|Llama|Mistral|fine.?tun"),
        ("topic:embeddings", r"embedding|vector|semantic|similarity|e5.?base"),
        ("topic:rag", r"RAG|retrieval|augmented generation|chunking|retriev"),
        ("topic:prompt-engineering", r"prompt|few.?shot|chain.?of.?thought|instruction"),
        ("topic:vector-db", r"vector.?db|pgvector|Qdrant|Pinecone|Weaviate|HNSW"),
        ("topic:agents", r"agent|multi.?agent|orchestrat|specialist|workflow"),
        ("topic:nlp", r"NLP|classification|categoriz|sentiment|tokeniz"),
        ("topic:ml-ops", r"MLOps|model.?deploy|inference|batch|benchmark|experiment"),
    ]),
    ("category:product", [
        ("topic:roadmap", r"roadmap|OKR|objective|key result|quarter|Q[1-4]"),
        ("topic:user-stories", r"user story|acceptance criteria|feature|persona"),
        ("topic:metrics", r"metric|KPI|MAU|DAU|churn|NPS|conversion|retention"),
        ("topic:launches", r"launch|release|rollout|feature flag|A/B test"),
        ("topic:feedback", r"feedback|customer|user research|survey|sentiment"),
        ("topic:prioritization", r"priorit|RICE|backlog|scoring|roadmap"),
    ]),
    ("category:personal", [
        ("topic:goals", r"goal|resolution|habit|routine|reflection|weekly review"),
        ("topic:productivity", r"productiv|pomodoro|deep work|focus|time management"),
        ("topic:finance", r"budget|invest|saving|expense|portfolio|revenue|MRR"),
        ("topic:health", r"health|exercise|workout|sleep|fitness|ergonomic|meditation"),
        ("topic:cooking", r"cook|recipe|bread|sourdough|kitchen|food"),
        ("topic:ideas", r"side project|idea|brainstorm|experiment"),
    ]),
    ("category:books", [
        ("topic:book-summary", r"book summary|book review|reading|key takeaway|rating.*\/5"),
        ("topic:quotes", r"quote|said|wrote|\u201c|\u201d|>>"),
        ("topic:reading-list", r"reading list|to.?read|planned|in progress|completed"),
    ]),
    ("category:learning", [
        ("topic:tutorial", r"tutorial|how.?to|step.?by.?step|guide|walkthrough"),
        ("topic:til", r"TIL|today I learned|just learned|discovered"),
        ("topic:cheatsheet", r"cheatsheet|cheat sheet|quick reference|commands"),
        ("topic:course-notes", r"course|lecture|notes from|lesson|module"),
    ]),
    ("category:research", [
        ("topic:benchmarks", r"benchmark|comparison|performance|vs\b|evaluated|tested"),
        ("topic:analysis", r"analysis|investigat|research|findings|experiment"),
        ("topic:architecture", r"architecture|design pattern|system design|diagram"),
    ]),
    ("category:news", [
        ("topic:tech-trends", r"trend|state of|report|industry|prediction|future"),
        ("topic:saved-articles", r"saved|article|blog post|notes from|annotati"),
    ]),
    ("category:travel", [
        ("topic:trip-notes", r"trip|travel|city|restaurant|food|hotel|flight"),
        ("topic:packing", r"packing|luggage|backpack|gear"),
        ("topic:places", r"Lisbon|Portugal|Tokyo|Berlin|Paris|Barcelona|Prague|Amsterdam|Seoul|Vienna|Copenhagen"),
    ]),
    ("category:design-ux", [
        ("topic:design-system", r"design system|Storybook|component library|token|Figma"),
        ("topic:accessibility", r"WCAG|accessibility|a11y|screen reader|ARIA|contrast"),
        ("topic:user-research", r"user research|usability|interview|persona|onboarding"),
        ("topic:ui-patterns", r"empty state|error state|loading|skeleton|modal|toast"),
        ("topic:figma", r"Figma|branch|handoff|component|variant"),
    ]),
    ("category:devops-sre", [
        ("topic:incident-response", r"incident|outage|postmortem|SEV[1-4]|PagerDuty|on.?call"),
        ("topic:slo", r"SLO|SLI|SLA|error budget|burn rate|availability.*99"),
        ("topic:chaos-engineering", r"chaos|game day|failover|fault injection|resilience"),
        ("topic:on-call", r"on.?call|rotation|escalat|runbook|page"),
        ("topic:postmortem", r"postmortem|post.?mortem|blameless|root cause|timeline"),
    ]),
    ("category:career-management", [
        ("topic:one-on-one", r"1.?on.?1|one.?on.?one|check.?in|feedback"),
        ("topic:hiring", r"hiring|interview|candidate|recruit|pipeline|offer"),
        ("topic:team-culture", r"team culture|values|ritual|onboard|code review culture"),
        ("topic:code-review-culture", r"code review|PR review|pull request|review culture|nit:"),
        ("topic:tech-leadership", r"staff engineer|tech lead|leadership|architect|mentor"),
    ]),
]

# Project simulation tags (some docs belong to "projects")
PROJECT_RULES = [
    ("project:mesh-api", r"Mesh|semantic search|document.*store|embedding.*service"),
    ("project:auth-service", r"auth.*service|authentication.*system|JWT.*service|login.*system"),
    ("project:dashboard", r"dashboard|analytics.*panel|admin.*panel|metrics.*view"),
    ("project:mobile-app", r"mobile.*app|iOS|Android|React Native|mobile.*backend"),
    ("project:docs-platform", r"documentation.*platform|knowledge.*base|docs.*system"),
]

# Status tags for some documents
STATUS_PATTERNS = [
    ("status:completed", r"completed|done|finished|shipped|deployed|launched"),
    ("status:in-progress", r"in progress|working on|currently|ongoing|WIP"),
    ("status:planned", r"planned|will|next quarter|upcoming|scheduled|todo"),
    ("status:draft", r"draft|preliminary|initial|rough|WIP|brainstorm"),
]

# Priority tags
PRIORITY_PATTERNS = [
    ("priority:high", r"critical|urgent|high priority|blocker|P0|P1"),
    ("priority:medium", r"medium priority|should|important|P2"),
    ("priority:low", r"nice.?to.?have|low priority|backlog|someday|P3"),
]


def classify_document(content: str, existing_tags: list) -> list:
    """Classify document and return new tags to add."""
    new_tags = []
    content_lower = content.lower()
    categories_found = set()

    # Category + topic classification
    for category_tag, topic_rules in CATEGORY_RULES:
        category_matched = False
        for topic_tag, pattern in topic_rules:
            if re.search(pattern, content, re.IGNORECASE):
                if topic_tag not in existing_tags:
                    new_tags.append(topic_tag)
                category_matched = True

        if category_matched and category_tag not in existing_tags:
            if category_tag not in categories_found:
                new_tags.append(category_tag)
                categories_found.add(category_tag)

    # Project tags (max 1 project per doc)
    for project_tag, pattern in PROJECT_RULES:
        if re.search(pattern, content, re.IGNORECASE):
            if project_tag not in existing_tags:
                new_tags.append(project_tag)
            break

    # Status tags (max 1)
    for status_tag, pattern in STATUS_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            # Don't add if already has a status tag
            if not any(t.startswith("status:") for t in existing_tags + new_tags):
                new_tags.append(status_tag)
            break

    # Priority tags (max 1, only for tasks/rfc/decision)
    has_actionable_type = any(
        t in existing_tags for t in ["type:task", "type:rfc", "type:decision"]
    )
    if has_actionable_type:
        for prio_tag, pattern in PRIORITY_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                if not any(t.startswith("priority:") for t in existing_tags + new_tags):
                    new_tags.append(prio_tag)
                break

    # Deduplicate
    seen = set(existing_tags)
    unique_new = []
    for t in new_tags:
        if t not in seen:
            unique_new.append(t)
            seen.add(t)

    return unique_new


def main():
    parser = argparse.ArgumentParser(description="Enrich demo documents with tags")
    parser.add_argument("--api", default="http://localhost:8001", help="Mesh API URL")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--limit", type=int, default=1000, help="Max documents to process")
    args = parser.parse_args()

    api = args.api.rstrip("/")

    all_docs = {}
    search_queries = [
        "software development code",
        "infrastructure docker server",
        "AI machine learning",
        "product roadmap metrics",
        "personal notes goals",
        "book reading summary",
        "news article trend",
        "research analysis benchmark",
        "learning tutorial guide",
        "finance business budget",
        "health productivity",
        "travel trip experience",
        "design architecture system",
        "implementation feature",
        "debugging fix bug",
        "review notes reflection",
    ]

    print("Fetching all documents via multiple searches...")
    for q in search_queries:
        try:
            resp = requests.post(f"{api}/search", json={
                "query": q,
                "limit": 100,
            }, timeout=30)
            if resp.status_code == 200:
                for r in resp.json().get("results", []):
                    guid = r.get("guid")
                    if guid and guid not in all_docs:
                        all_docs[guid] = r
        except Exception:
            pass

    print(f"Total unique documents: {len(all_docs)}")

    # Classify and update
    updated = 0
    skipped = 0
    errors = 0

    for i, (guid, doc) in enumerate(all_docs.items()):
        content = doc.get("content", "")
        existing_tags = doc.get("tags", [])

        new_tags = classify_document(content, existing_tags)

        if not new_tags:
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [{guid[:8]}] +{new_tags}")
            updated += 1
            continue

        try:
            resp = requests.patch(f"{api}/{guid}", json={
                "add_tags": new_tags,
            }, timeout=10)
            if resp.status_code == 200:
                updated += 1
            else:
                errors += 1
                if errors <= 3:
                    print(f"  ERROR [{guid[:8]}]: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ERROR [{guid[:8]}]: {e}")

        # Progress
        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{len(all_docs)} (updated: {updated}, skipped: {skipped})")

        # Don't hammer the API
        if not args.dry_run and (i + 1) % 20 == 0:
            time.sleep(0.2)

    print(f"\nDone: {updated} updated, {skipped} skipped, {errors} errors")

    # Show tag distribution after
    if not args.dry_run:
        time.sleep(1)
        resp = requests.get(f"{api}/tags", timeout=10)
        if resp.status_code == 200:
            tags = resp.json().get("tags", [])
            cats = [t for t in tags if t["tag"].startswith("category:")]
            topics = [t for t in tags if t["tag"].startswith("topic:")]
            projects = [t for t in tags if t["tag"].startswith("project:")]
            statuses = [t for t in tags if t["tag"].startswith("status:")]
            print(f"\nTag summary:")
            print(f"  Categories: {len(cats)}")
            for t in sorted(cats, key=lambda x: -x['count']):
                print(f"    {t['tag']}: {t['count']}")
            print(f"  Topics: {len(topics)}")
            for t in sorted(topics, key=lambda x: -x['count'])[:20]:
                print(f"    {t['tag']}: {t['count']}")
            if len(topics) > 20:
                print(f"    ... and {len(topics)-20} more")
            print(f"  Projects: {len(projects)}")
            for t in projects:
                print(f"    {t['tag']}: {t['count']}")
            print(f"  Statuses: {len(statuses)}")
            for t in statuses:
                print(f"    {t['tag']}: {t['count']}")


if __name__ == "__main__":
    main()
