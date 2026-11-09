"""
Smart Node voice adapter — voice channel for a Smart Node, Gemini Live API.
Full duplex: audio in -> Gemini -> audio out.
Per-user identity via UUID. Identity/sessions/logs in SQLite; per-user
memory (profile + queue) lives in the node memory companion (Mesh).
Unified message queue: profile + queue (learn/remind/system/feedback).

v1 monolith forked from Voice. Answer-now
scope only — no task delegation yet. Identity is configurable via ASSISTANT_NAME.
"""
import os
import json
import asyncio
import uuid
import time
import aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

# --- Config ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025")
# The text model that reads a finished call and extracts what to remember. It is
# NOT the live audio model above, and it is deliberately an ALIAS: this used to
# be pinned to gemini-2.5-flash, which Google withdrew ("no longer available to
# new users"), and every call since then silently remembered nothing. An alias
# moves with the provider instead of dying with one release. Override only to
# pin a specific version on purpose.
EXTRACT_MODEL = os.environ.get("VOICE_EXTRACT_MODEL", "gemini-flash-latest")
DB_PATH = os.environ.get("DB_PATH", "/data/voice.db")
ADMIN_TOKEN = os.environ.get("VOICE_ADMIN_TOKEN", "")
ACCESS_PHRASE = os.environ.get("VOICE_ACCESS_PHRASE", "")
MAX_SESSIONS_PER_HOUR = int(os.environ.get("VOICE_MAX_SESSIONS_HOUR", "20"))

# Node voice identity (rebrandable -- replaces Voice branding)
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "assistant")

# The language of a CALL. The realtime API takes ONE language_code per session
# and it decides recognition as much as speech, so this is not a preference the
# model can talk its way out of mid-call -- which is why every prompt below is
# rendered in the language resolved here rather than hardcoded.
#
# The CALLER decides when we know them: `language` is a profile field the memory
# extractor already fills in from how they actually speak. VOICE_LANGUAGE is the
# fallback for a caller we have never met, and it stays English so a business
# line does not answer a stranger in the owner's language.
VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "en-US").strip() or "en-US"

# BCP-47 code, English name, and the spellings a profile might hold -- the field
# is free text written by a model in whatever language the caller spoke, so
# "Russian", "RU" and "ru" all have to land in the same place.
_LANGUAGES = (
    ("en-US", "English",    ("en", "eng", "english")),
    ("ru-RU", "Russian",    ("ru", "rus", "russian")),
    ("uk-UA", "Ukrainian",  ("uk", "ukr", "ua", "ukrainian")),
    ("de-DE", "German",     ("de", "ger", "german", "deutsch")),
    ("fr-FR", "French",     ("fr", "fra", "french", "français")),
    ("es-ES", "Spanish",    ("es", "spa", "spanish", "español")),
    ("pl-PL", "Polish",     ("pl", "pol", "polish", "polski")),
    ("it-IT", "Italian",    ("it", "ita", "italian", "italiano")),
    ("pt-BR", "Portuguese", ("pt", "por", "portuguese", "português")),
)


def _language_by_code(code):
    """(code, name) for a BCP-47 code, matching on the language part so `ru`,
    `ru-RU` and `ru_ru` all resolve. Unknown but well-formed codes are passed
    through with the code as their own name -- the API knows more languages than
    this table lists, and refusing one we simply have not named would be worse
    than speaking it."""
    c = (code or "").strip().replace("_", "-")
    if not c:
        return None
    head = c.split("-", 1)[0].lower()
    for full, name, _aliases in _LANGUAGES:
        if full.lower() == c.lower() or full.split("-", 1)[0] == head:
            return (full, name)
    return (c, c) if head.isalpha() and 2 <= len(head) <= 3 else None


def resolve_call_language(profile):
    """(code, name) for this call: the caller's own language when their profile
    names one, else the node default. Never raises and never returns empty --
    an unreadable profile falls back rather than leaving the session with no
    language at all."""
    try:
        raw = (profile or {}).get("language")
        raw = raw.strip().lower() if isinstance(raw, str) else ""
    except Exception:
        raw = ""
    if raw:
        for full, name, aliases in _LANGUAGES:
            if raw == name.lower() or raw in aliases or raw.startswith(tuple(aliases)):
                return (full, name)
        byc = _language_by_code(raw)
        if byc:
            return byc
    return _language_by_code(VOICE_LANGUAGE) or ("en-US", "English")
# Internal label for the assistant's turns in the stored transcript
BOT_LABEL = "ASSISTANT"
_BOT_PREFIX = BOT_LABEL + ": "

# Node memory companion (Mesh) -- user memory lives here, not in local
# SQLite. On a node this resolves to the smart-node-mesh
# container on the smart-node network; the companion runs AUTH_REQUIRED=
# false so MESH_API_KEY is normally empty. Same convention as brin/dispatcher.
MESH_API_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")
MESH_WORKSPACE = os.environ.get("MESH_WORKSPACE", "voice")
MESH_API_KEY = os.environ.get("MESH_API_KEY", "")
MESH_TIMEOUT = float(os.environ.get("MESH_TIMEOUT", "5"))



INPUT_PRICE = 3.0     # $ per 1M input audio tokens
OUTPUT_PRICE = 12.0   # $ per 1M output audio tokens
BYTES_PER_TOKEN_IN = 800
BYTES_PER_TOKEN_OUT = 1200

VOICES = {
    "aoede": {"id": "Aoede", "desc": "warm female", "lang": "multilingual"},
    "charon": {"id": "Charon", "desc": "deep male", "lang": "multilingual"},
    "fenrir": {"id": "Fenrir", "desc": "calm male", "lang": "multilingual"},
    "kore": {"id": "Kore", "desc": "bright female", "lang": "multilingual"},
    "puck": {"id": "Puck", "desc": "friendly male", "lang": "multilingual"},
    "orus": {"id": "Orus", "desc": "steady male", "lang": "multilingual"},
    "leda": {"id": "Leda", "desc": "gentle female", "lang": "multilingual"},
}
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"



def validate_uid(uid: str) -> str:
    """Validate UID is a proper UUID format."""
    try:
        return str(uuid.UUID(uid))
    except (ValueError, AttributeError):
        return str(uuid.uuid4())

# --- Database ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                preferences TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_s REAL DEFAULT 0,
                input_bytes INTEGER DEFAULT 0,
                output_bytes INTEGER DEFAULT 0,
                input_tokens REAL DEFAULT 0,
                output_tokens REAL DEFAULT 0,
                cost REAL DEFAULT 0,
                turns INTEGER DEFAULT 0,
                voice TEXT DEFAULT 'Aoede',
                FOREIGN KEY (uid) REFERENCES users(uid)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                msg_type TEXT NOT NULL,
                size_bytes INTEGER DEFAULT 0,
                text_content TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
            CREATE INDEX IF NOT EXISTS idx_logs_session ON logs(session_id);
        """)


async def migrate_db():
    """Add new columns if not exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        cols = [r[1] for r in await db.execute_fetchall("PRAGMA table_info(users)")]
        if "memory" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN memory TEXT DEFAULT '{}'")
            print("[DB] Added memory column")
        if "verified" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0")
            print("[DB] Added verified column")
        await db.commit()


async def get_or_create_user(uid: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute_fetchall("SELECT * FROM users WHERE uid = ?", (uid,))
        if row:
            await db.execute("UPDATE users SET last_seen = ? WHERE uid = ?", (now, uid))
            await db.commit()
            r = dict(row[0])
            r["preferences"] = json.loads(r["preferences"] or "{}")
            return r
        else:
            await db.execute(
                "INSERT INTO users (uid, name, preferences, created_at, last_seen) VALUES (?, '', '{}', ?, ?)",
                (uid, now, now)
            )
            await db.commit()
            return {"uid": uid, "name": "", "preferences": {}, "verified": 0, "created_at": now, "last_seen": now}


async def update_user(uid: str, name: str = None, preferences: dict = None, verified: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if name is not None:
            await db.execute("UPDATE users SET name = ? WHERE uid = ?", (name[:100], uid))
        if preferences is not None:
            await db.execute("UPDATE users SET preferences = ? WHERE uid = ?", (json.dumps(preferences), uid))
        if verified is not None:
            await db.execute("UPDATE users SET verified = ? WHERE uid = ?", (verified, uid))
        await db.commit()


# --- Memory (node memory companion) ---
# A user's memory is one Mesh document in the node memory companion. The
# GUID is derived from the user's UUID, so the document can be fetched
# and upserted without a lookup. The document body is the Voice
# {profile, queue} JSON verbatim -- a shim, per the earlier change plan; the rest
# of the memory pipeline (migrate/cleanup/extract/build_system_prompt) is
# storage-agnostic and unchanged. If the companion is unreachable, a call
# degrades to in-session-only memory rather than failing.

def _mesh_headers() -> dict:
    h = {"X-Workspace": MESH_WORKSPACE}
    if MESH_API_KEY:
        h["X-API-Key"] = MESH_API_KEY
    return h


def _memory_guid(uid: str) -> str:
    """Deterministic Mesh GUID for a user's memory document.

    uid is a validated UUID; the first 16 hex chars give the canonical
    20-char `doc_` GUID -- matches Mesh's ^doc_[0-9a-f]{8,32}$ pattern
    and fits the guid VARCHAR(32) column."""
    return "doc_" + uuid.UUID(uid).hex[:16]


async def get_user_memory(uid: str) -> dict:
    """Load a user's memory document from the node memory companion."""
    try:
        guid = _memory_guid(uid)
        async with httpx.AsyncClient(timeout=MESH_TIMEOUT, verify=False) as client:
            resp = await client.get(f"{MESH_API_URL}/{guid}", headers=_mesh_headers())
        if resp.status_code == 404:
            return {"profile": {}, "queue": []}
        resp.raise_for_status()
        memory = json.loads(resp.json().get("content", "") or "{}")
        return migrate_memory_format(memory)
    except Exception as e:
        print(f"[Memory] Mesh load failed for {uid[:8]} ({MESH_API_URL}): {e}")
        return {"profile": {}, "queue": []}


async def save_user_memory(uid: str, memory: dict):
    """Persist a user's memory document to the node memory companion."""
    try:
        guid = _memory_guid(uid)
        payload = {
            "content": json.dumps(memory, ensure_ascii=False),
            "tags": ["type:voice-memory", f"uid:{uid}", "agent:smart-node-voice"],
            "source": "voice",
        }
        async with httpx.AsyncClient(timeout=MESH_TIMEOUT, verify=False) as client:
            resp = await client.put(f"{MESH_API_URL}/doc/{guid}",
                                    json=payload, headers=_mesh_headers())
        resp.raise_for_status()
    except Exception as e:
        print(f"[Memory] Mesh save failed for {uid[:8]} ({MESH_API_URL}): {e}")



def migrate_memory_format(memory: dict) -> dict:
    """Lazy migration: convert old 3-layer format to unified queue."""
    if "queue" in memory:
        return memory  # already migrated

    queue = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # short_term -> remind
    for e in memory.get("short_term", []):
        queue.append({
            "id": str(uuid.uuid4()),
            "direction": "remember", "subtype": "remind",
            "category": "general", "text": e.get("fact", ""),
            "created": e.get("created", now_iso),
            "expires": e.get("expires"),
            "max_shows": 5, "shown_count": 0, "status": "active"
        })

    # long_term -> learn (no expires) or remind (with expires)
    for e in memory.get("long_term", []):
        has_exp = bool(e.get("expires"))
        queue.append({
            "id": str(uuid.uuid4()),
            "direction": "remember",
            "subtype": "remind" if has_exp else "learn",
            "category": e.get("tags", ["general"])[0] if e.get("tags") else "general",
            "text": e.get("fact", ""),
            "created": e.get("created", now_iso),
            "expires": e.get("expires") if has_exp else None,
            "max_shows": 5 if has_exp else None,
            "shown_count": 0, "status": "active"
        })

    # last_conversations -> system/conversation_summary
    for e in memory.get("last_conversations", []):
        queue.append({
            "id": str(uuid.uuid4()),
            "direction": "notify", "subtype": "system",
            "category": "conversation_summary",
            "text": e.get("summary", ""),
            "created": e.get("date", now_iso),
            "expires": None, "max_shows": 3,
            "shown_count": 0, "status": "active"
        })

    return {"profile": memory.get("profile", {}), "queue": queue}

def cleanup_memory(memory: dict) -> dict:
    """Clean up expired/done/shown-out queue entries."""
    now = datetime.now(timezone.utc).isoformat()
    queue = memory.get("queue", [])

    cleaned = []
    seen_learn = {}  # category -> index in cleaned (for dedup)

    for entry in queue:
        # Skip done
        if entry.get("status") in ("done", "expired"):
            continue
        # Skip shown-out (except learn which has no max_shows)
        if entry.get("max_shows") is not None and entry.get("shown_count", 0) >= entry["max_shows"]:
            continue
        # Skip remind entries scheduled for later (next_show)
        if entry.get("next_show") and entry["next_show"] > now:
            if entry.get("subtype") == "remind":
                continue
        # Mark expired: reminds as "missed" for prompt, others removed
        if entry.get("expires") and entry["expires"] < now:
            if entry.get("subtype") == "remind":
                entry["_missed"] = True
            else:
                continue

        # LEARN: deduplicate by category (keep newest)
        if entry.get("subtype") == "learn":
            entry["expires"] = None  # enforce never-expires
            cat = entry.get("category", "general")
            if cat in seen_learn:
                old_idx = seen_learn[cat]
                if entry.get("created", "") > cleaned[old_idx].get("created", ""):
                    cleaned[old_idx] = entry
                continue
            seen_learn[cat] = len(cleaned)

        cleaned.append(entry)

    # Cap at 100
    if len(cleaned) > 100:
        cleaned = cleaned[-100:]

    memory["queue"] = cleaned
    return memory


async def extract_memory_from_session(uid: str, thinking_texts: list, transcript: list = None):
    """After session ends, extract structured memory from thinking texts + user transcript."""
    if not thinking_texts and not transcript:
        return

    memory = await get_user_memory(uid)
    profile = memory.get("profile", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()

    all_thinking = "\n---\n".join(thinking_texts[-30:]) if thinking_texts else ""

    # User's actual words from browser SpeechRecognition
    user_transcript = ""
    if transcript:
        # Merge syllable fragments into coherent lines
        merged = []
        current_speaker = None
        current_text = ""
        for line in transcript:
            if line.startswith("USER: "):
                if current_speaker and current_speaker != "USER":
                    merged.append(f"{current_speaker}: {current_text.strip()}")
                    current_text = ""
                current_speaker = "USER"
                current_text += line[6:]
            elif line.startswith(_BOT_PREFIX):
                if current_speaker and current_speaker != BOT_LABEL:
                    merged.append(f"{current_speaker}: {current_text.strip()}")
                    current_text = ""
                current_speaker = BOT_LABEL
                current_text += line[len(_BOT_PREFIX):]
            else:
                current_text += line
        if current_speaker and current_text.strip():
            merged.append(f"{current_speaker}: {current_text.strip()}")
        user_transcript = "\n".join(merged[-30:])

    # Build existing queue context for dedup + voice control
    existing_entries = [e.get("text", "")[:80] for e in memory.get("queue", []) if e.get("status") == "active"]
    # For queue_updates: provide id + text so extraction can reference specific entries
    existing_with_ids = [
        {"id": e.get("id", ""), "subtype": e.get("subtype", ""), "category": e.get("category", ""), "text": e.get("text", "")[:80]}
        for e in memory.get("queue", [])
        if e.get("status") == "active" and e.get("subtype") in ("remind", "learn")
    ]

    prompt = (
        "You are a memory extraction system. Analyze conversation data and extract ONLY meaningful personal facts.\n\n"
        f"TODAY: {today}\n\n"
        "CURRENT USER PROFILE:\n"
        f"{json.dumps(profile, ensure_ascii=False) if profile else 'empty - new user'}\n\n"
        "EXISTING MEMORY (do NOT duplicate):\n"
        f"{json.dumps(existing_entries[-15:], ensure_ascii=False) if existing_entries else 'none'}\n\n"
        "EXISTING ENTRIES WITH IDs (for queue_updates):\n"
        f"{json.dumps(existing_with_ids[-15:], ensure_ascii=False) if existing_with_ids else 'none'}\n\n"
        "CONVERSATION NOTES (model's thinking during voice session):\n"
        f"{all_thinking}\n\n"
        + (f"USER'S ACTUAL WORDS (transcribed from speech):\n{user_transcript}\n\n" if user_transcript else "")
        + "TASK: Return a JSON object with these fields:\n\n"
        '1. "profile_updates" - object with any NEW or CHANGED profile fields. Only include fields with new info.\n'
        "   Possible fields: name, language, location, mood, personality, interests (array), family (object), health, occupation\n\n"
        '2. "new_entries" - array of memory queue entries. Each:\n'
        '   {"direction": "remember", "subtype": "learn" or "remind", "category": "string", "text": "...", "expires": "ISO" or null}\n'
        "   Rules:\n"
        '   - learn = permanent preference/fact. expires MUST be null.\n'
        '   - remind = time-bound action/reminder. Set expires.\n'
        '   - category: preference, english_learning, health, work, hobby, family, food, certification, general\n'
        '   - If the user said remember/learn (any language) -- capture word-for-word.\n'
        '   - DO NOT extract: weather questions, technical events, access control.\n'
        '   - DO NOT duplicate existing memory entries.\n\n'
        '3. "queue_updates" - array of updates to EXISTING entries. Use when user wants to change or cancel a reminder/fact.\n'
        '   {"id": "existing-entry-id", "action": "done" or "update", "text": "new text if update", "expires": "new ISO if update"}\n'
        '   Detect user intent:\n'
        '   - "stop reminding" / "enough" / "cancel" (any language) -> action: "done"\n'
        '   - "remind more often" (any language) -> action: "update" (you cannot change frequency, use action "done" + add new entry in new_entries)\n'
        '   - "remind me in a week" (any language) -> action: "update" with new expires\n'
        '   - Match the user\'s words to the closest entry in EXISTING ENTRIES WITH IDs by meaning.\n'
        '   - If no match found, skip. Do NOT guess.\n\n'
        '4. "capability_requests" - array of capability gap requests the user wants forwarded to the developer.\n'
        '   Each item: {"text": "short description of what user wants", "context": "1-2 sentence context from conversation"}\n'
        "   Create entry ONLY if ALL of these are true:\n"
        '   - User asked Voice to do something Voice cannot do (check balance, adjust volume, check email, call somebody, control device, etc)\n'
        '   - Voice offered to forward the request to the developer (said something like "I will forward this to the developer")\n'
        '   - User agreed (said yes / agreed, any language)\n'
        "   If any of the three is missing -- DO NOT create the entry. In particular:\n"
        '   - Do NOT create for things Voice CAN do (translate, answer questions, chat).\n'
        '   - Do NOT create if user declined or did not explicitly agree.\n'
        '   - Do NOT duplicate: if EXISTING ENTRIES already contain a similar capability_request -- skip.\n'
        '   Text: concise functional description in the user language ("adjust the phone volume", "check mobile balance").\n\n'
        '5. "conversation_summary" - 1-2 sentence summary of what was discussed and where the conversation ended.\n'
        "   Focus on: what topics came up, what the user was doing/feeling, what was left unfinished.\n"
        "   Example: \"Asked about the status of a request from last week, then moved on to next week's schedule.\"\n\n"
        'Return ONLY valid JSON. If nothing found, return {"profile_updates": {}, "new_entries": [], "queue_updates": [], "capability_requests": [], "conversation_summary": ""}\n'
        "Write facts in the language the user spoke."
    )

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{EXTRACT_MODEL}:generateContent?key={GOOGLE_API_KEY}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"}
            })
            # Say WHY out loud. Reading ["candidates"] off a failed response
            # raises KeyError('candidates'), which is what this looked like for
            # weeks -- a puzzling key error that hid a plain 404 naming the
            # withdrawn model. An HTTP failure here is a configuration fact, so
            # it is reported as one.
            if resp.status_code != 200:
                print("[Memory] extraction model %s -> HTTP %s: %s"
                      % (EXTRACT_MODEL, resp.status_code, resp.text[:300]))
                return
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)

            # Apply profile updates (merge)
            updates = result.get("profile_updates", {})
            if updates:
                for k, v in updates.items():
                    if k == "interests" and isinstance(v, list):
                        existing = profile.get("interests", [])
                        profile["interests"] = list(dict.fromkeys(existing + v))
                    elif k == "family" and isinstance(v, dict):
                        existing_fam = profile.get("family", {})
                        existing_fam.update(v)
                        profile["family"] = existing_fam
                    else:
                        profile[k] = v

            # Add new queue entries
            new_entries = result.get("new_entries", [])
            queue = memory.get("queue", [])
            for entry in new_entries:
                subtype = entry.get("subtype", "learn")
                queue.append({
                    "id": str(uuid.uuid4()),
                    "direction": entry.get("direction", "remember"),
                    "subtype": subtype,
                    "category": entry.get("category", "general"),
                    "text": entry["text"],
                    "created": now_iso,
                    "expires": entry.get("expires"),
                    "max_shows": None if subtype == "learn" else 5,
                    "shown_count": 0,
                    "status": "active"
                })

            # Apply queue updates (voice control of reminders)
            queue_updates = result.get("queue_updates", [])
            if queue_updates:
                queue_by_id = {e.get("id"): e for e in queue if e.get("id")}
                for upd in queue_updates:
                    entry_id = upd.get("id", "")
                    action = upd.get("action", "")
                    if entry_id in queue_by_id:
                        if action == "done":
                            queue_by_id[entry_id]["status"] = "done"
                            print(f"[Memory] Queue update: {entry_id[:8]} -> done")
                        elif action == "update":
                            if upd.get("text"):
                                queue_by_id[entry_id]["text"] = upd["text"]
                            if upd.get("expires"):
                                queue_by_id[entry_id]["expires"] = upd["expires"]
                            print(f"[Memory] Queue update: {entry_id[:8]} -> updated")

            # Capability requests -- saved after main memory save (see below)
            capability_requests = result.get("capability_requests", [])

            # Conversation summary as queue entry
            summary = result.get("conversation_summary", "")
            if summary:
                queue.append({
                    "id": str(uuid.uuid4()),
                    "direction": "notify", "subtype": "system",
                    "category": "conversation_summary",
                    "text": summary, "created": now_iso,
                    "expires": None, "max_shows": 3,
                    "shown_count": 0, "status": "active"
                })

            # Save
            memory["queue"] = queue
            memory = cleanup_memory(memory)
            await save_user_memory(uid, memory)

            # Backstop: create capability_requests detected by extraction
            # (in case inline thinking tags were missed). create_capability_request
            # handles its own dedup, load, save and Telegram delivery.
            for cr in capability_requests:
                cr_text = (cr.get("text") or "").strip()
                if len(cr_text) < 3:
                    continue
                ctx = (cr.get("context") or "").strip()
                ctx_lines = [ctx] if ctx else [user_transcript[-400:]] if user_transcript else ["(no context)"]
                try:
                    await create_capability_request(uid, cr_text, ctx_lines)
                except Exception as e:
                    print(f"[Memory] capability_request delivery failed: {e}")

            # Also update user name if extracted
            if updates.get("name"):
                user = await get_or_create_user(uid)
                if not user.get("name"):
                    await update_user(uid, name=updates["name"])

            # Auto-verify only if extraction found a real user name
            # (summary alone is not enough -- any conversation produces a summary,
            # even one where the user just repeated the access phrase)
            if updates.get("name"):
                user = await get_or_create_user(uid)
                if not user.get("verified"):
                    await update_user(uid, verified=1)
                    print(f"[Memory] Auto-verified user {uid[:8]} (name extracted: {updates['name']})")

            print(f"[Memory] Updated for {uid}: profile={list(updates.keys())}, +{len(new_entries)} entries, summary={bool(summary)}")
    except Exception as e:
        print(f"[Memory] Extraction failed: {e}")


# --- Session ---
async def create_session(uid: str, voice: str) -> str:
    now = datetime.now(timezone.utc).isoformat()
    session_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (id, uid, started_at, voice) VALUES (?, ?, ?, ?)",
            (session_id, uid, now, voice)
        )
        await db.commit()
        return session_id


async def end_session(session_id: int, stats: dict):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE sessions SET ended_at=?, duration_s=?, input_bytes=?, output_bytes=?,
               input_tokens=?, output_tokens=?, cost=?, turns=? WHERE id=?""",
            (now, stats["duration"], stats["input_bytes"], stats["output_bytes"],
             stats["input_tokens"], stats["output_tokens"], stats["cost"], stats["turns"],
             session_id)
        )
        await db.commit()


async def log_message(session_id: int, direction: str, msg_type: str, size: int = 0, text: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (session_id, ts, direction, msg_type, size_bytes, text_content) VALUES (?,?,?,?,?,?)",
            (session_id, now, direction, msg_type, size, text[:500])
        )
        await db.commit()


# --- System Prompt ---
def _extract_md_section(md: str, header: str) -> str:
    """Lift ONE top-level markdown section (header line through the line before the
    next top-level '# ' header). Used to take the # Pinned knowledge block out of the
    composed CLAUDE.md WITHOUT dragging in the tool/inbox mechanics -- those are
    channel-specific and replaced by the voice transport layer."""
    # Sections are composed as "---

# Header"; a REAL section boundary is a "# "
    # line whose preceding non-blank line was a "---" separator. Do NOT break on "# "
    # headers INSIDE pinned document content (a doc whose body starts with "# Title") --
    # that bug truncated the pinned product lineup and made the voice agent confabulate.
    out, cap, prev_sep = [], False, False
    for ln in (md or '').splitlines():
        if ln.startswith(header):
            cap = True; out.append(ln); prev_sep = False; continue
        if cap and ln.startswith('# ') and not ln.startswith(header) and prev_sep:
            break
        if cap:
            out.append(ln)
        _st = ln.strip()
        if _st:
            prev_sep = (_st == '---')
    return (chr(10).join(out)).strip()


async def _fetch_persona_core(slug: str):
    """Assemble the voice prompt from the SAME channel-neutral core every channel
    uses (the user's idea: identical composition per channel). Pulls from core-api:
      core   = specialist_body (identity + business rules),
      pinned = the # Pinned knowledge block of the composed CLAUDE.md.
    Only the transport layer (brevity, set_voice, ask_brin, no-tools) is added on top
    per channel. Bounded for the realtime prompt. Fail soft -> ('','')."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{CORE_API_URL}/api/agents/{slug}", params={"body": "true"},
                            headers=_owner_read_headers())
        if r.status_code != 200:
            return ('', '', '')
        d = r.json() or {}
        # WHO the agent is, not the manual for how it works. A specialist body is
        # written for the desk -- identity in the opening paragraphs, then pages of
        # method (how to run an interview, how to decide what to propose). On a call
        # only the identity is any use, and the method crowds out the rules that
        # matter here. Cutting at a character count instead sliced a sentence in
        # half and shipped the front half of a doctrine; cutting at the first
        # sub-heading gives a whole, self-contained introduction.
        _body = (d.get("specialist_body") or "").strip()
        _head = _body.split("\n## ", 1)[0].strip()
        # The name first, from the agent row. This line builds its identity from
        # the specialist body directly and never sees the composed CLAUDE.md, so
        # the name has to be put here too -- and from the same field,
        # sn_config.agents.title, not the slug this file used to capitalise.
        # Asked its name on a call, an agent whose role text does not happen to
        # contain it answered "Gemini".
        _name = (d.get("title") or "").strip()
        core = ("Your name is %s. It is the only name you give.\n\n%s"
                % (_name, _head)) if _name else _head
        if len(_body) > len(_head):
            print("[persona] %s: identity %d chars, method (%d) left at the desk"
                  % (slug, len(core), len(_body) - len(_head)))
        _bmd = d.get("body_md") or ""
        pinned = _extract_md_section(_bmd, "# Pinned knowledge")
        team = _extract_md_section(_bmd, "# Team")   # colleague roster (delegate by name)

        # Only what a caller can be answered from. The node's capability catalogue
        # and a prose list of the agent's tools used to be carried here too, and
        # both did harm: the catalogue is a configuration reference (it belongs at
        # the desk, not on a line) and the tool list named capabilities under
        # registry slugs that match none of the functions declared to this session,
        # so the assistant read "I have documents, memory, contacts" and concluded
        # that everything must be done by someone else. What it can call is already
        # stated by the function declarations; what it may not is one rule below.
        _about = []
        # Policies are the node's behaviour rules, kept in the DB and editable from
        # the panel. Voice never read them, so the same rule had to be written a
        # second time in this file, in different words, and the two drifted.
        for _h in ("# Policies", "# Who you may contact"):
            _sec = _extract_md_section(_bmd, _h)
            if _sec.strip():
                _about.append(_sec.strip())
        if _about:
            pinned = (pinned + "\n\n" + "\n\n".join(_about)).strip()
        # Nothing above is cut any more. These four blocks used to be sliced to a
        # fixed budget, silently and mid-word, so the prompt on a call was never
        # the prompt anyone wrote: 203 characters of policy went missing on every
        # call, and the lost tail was the rule that turns real work into a task
        # Their inputs are already bounded upstream -- the role at its
        # first sub-heading, pinned knowledge by MAX_PINNED_CHARS in the composer,
        # the roster by the number of agents -- so a second cut here bought
        # nothing and hid everything. Sizes are printed instead: growth stays
        # visible, and making a prompt smaller is a deliberate piece of work, not
        # something a slice does behind your back.
        print("[persona] %s: role %d, pinned+rules %d, team %d chars"
              % (slug, len(core), len(pinned), len(team)))
        return (core, pinned, team)
    except Exception as _e:
        print(f"[persona] core fetch failed for {slug}: {_e}")
        return ('', '', '')


# Gemini's function-declaration schema is a strict subset of JSON Schema: it
# rejects keys it does not know (a stray $schema fails the whole setup, which
# on this path means the call never connects). Keep only what it accepts.
_GEMINI_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum", "format", "nullable"}


def _gemini_schema(node):
    """Recursively strip a JSON Schema down to what Gemini Live accepts."""
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


async def fetch_catalog_tools(slug: str):
    """This agent's granted tools, as callable declarations.

    Same source brin uses (GET /api/agents/<slug>/tools): the DB catalogue is
    the single truth for what an agent may do, so a grant change reaches voice
    with no code edit here.

    Only `api` rows are taken, and only those carrying BOTH a real object schema
    and a path. That is deliberate: a row without a schema would reach the model
    as a function with no arguments -- callable, useless, and indistinguishable
    from a working tool until it is called. `builtin` is skipped (no shell by
    phone), `cli` cannot be dispatched from here, and `mcp` needs the server
    listed for its own schemas -- a separate step.

    Returns (declarations, dispatch_map). The catalogue's `usage_md` is NOT
    carried into the prompt: it is written for an agent with a shell ("curl -sS
    -H Remote-User..."), and telling a voice model to run curl invites it to
    narrate a command instead of calling the tool. The declaration's own
    description is what this channel needs.

    Fails soft to empty: losing a tool must never cost the call.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{CORE_API_URL}/api/agents/{slug}/tools",
                            headers=_owner_read_headers())
        if r.status_code != 200:
            print("[catalog] %s -> HTTP %s" % (slug, r.status_code))
            return ([], {})
        rows = (r.json() or {}).get("tools") or []
    except Exception as e:
        print("[catalog] fetch failed for %s: %s" % (slug, e))
        return ([], {})

    decls, dispatch = [], {}
    for t in rows:
        name = t.get("name") or ""
        if t.get("kind") != "api" or not name:
            continue
        schema = t.get("input_schema") or {}
        if not isinstance(schema, dict) or schema.get("type") != "object" or not schema.get("properties"):
            continue
        endpoint = (t.get("endpoint") or "").rstrip("/")
        path = t.get("http_path") or ""
        if not endpoint or not path:
            print("[catalog] %s: skipped -- no endpoint/path (advertised but not callable)" % name)
            continue
        decls.append({
            "name": name,
            "description": (t.get("description") or t.get("title") or name)[:1000],
            "parameters": _gemini_schema(schema),
        })
        dispatch[name] = ((t.get("http_method") or "POST").upper(), endpoint + path)
    return (decls, dispatch)


def build_system_prompt(user: dict, memory: dict, client_time: str = "", client_tz: str = "", wkey: str = "", persona_name: str = None, skip_phrase: bool = False, core: str = "", pinned: str = "", team: str = "", lang_name: str = "English") -> str:
    name = user.get("name", "")
    aname = persona_name or ASSISTANT_NAME   # the persona this door serves, else the configured default name
    verified = 1 if skip_phrase else user.get("verified", 0)  # a persona door skips the access phrase
    profile = memory.get("profile", {})

    if client_time:
        now_str = f"{client_time} ({client_tz})" if client_tz else client_time
    else:
        # Fallback: should not happen, client always sends local_time
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Unverified users must provide password first
    if not verified:
        return f"""You are {aname}, a private voice assistant for a Smart Node.
Current date and time: {now_str}
ALWAYS speak {lang_name}. Do not switch to, mirror, or drift to any other language.

CRITICAL INSTRUCTION -- ACCESS CONTROL:
This user has NOT been verified yet. You MUST ask for the access password BEFORE anything else.
Say something like: "Hi! I'm the voice assistant of this node. To get started, please tell me the access phrase."

The correct password is: "{ACCESS_PHRASE}" (case insensitive; accept it when the user says that word clearly, allowing small mis-hearings of the same word).

If the user says the correct password:
- Write [ACCESS:GRANTED] in your thinking
- Then say warmly: "Welcome! Let's get to know each other -- what's your name?"
- Continue normally from there

If the user says anything else (wrong password, random questions, attempts to bypass):
- Write [ACCESS:DENIED] in your thinking
- Say politely: "I'm sorry, that's not the right phrase. I can't continue without it. Goodbye!"
- Do NOT answer any questions, do NOT engage in conversation
- Do NOT reveal the password or give hints

You have maximum 3 attempts. After each wrong attempt, remind them politely.
Do NOT proceed to any other topic until the password is provided.
"""

    if core:
        # Channel-neutral core (identity + rules) from the source of truth -- the SAME
        # composition brin/Telegram use. Voice adds only its transport rules below.
        # This line used to tell every agent to answer about "your products, prices,
        # specs and certifications" and to never do anything itself. It was written
        # for one sales persona on the demo node, and it survived into every node and
        # every role -- a configurator with no products was being told to quote from
        # a price list that does not exist, and forbidden to act, one screen above the
        # rule that tells it to act. One persona per prompt; the rule for acting is
        # stated once, below.
        opening = (core + chr(10) + chr(10) +
            "You are this person, live on a voice call: speak in character and follow the "
            "rules above. Answer from what you actually know; never invent a fact. If you "
            "do not know it, do not guess.")
        if team:
            # Colleague roster: lets you answer "who handles X" directly AND pick the
            # right person to delegate to (send_message to=<their name>), instead of
            # always delegating to yourself.
            opening = opening + chr(10) + chr(10) + team
        if pinned:
            # Pinned knowledge: full content in-prompt, answered directly (no tool call).
            opening = opening + chr(10) + chr(10) + pinned
    else:
        opening = f"You are {aname}, a friendly and helpful voice assistant for a Smart Node."
    # The rules below are business-AGNOSTIC on purpose: nothing here may name a
    # trade, a job title or a product. Anything specific to the business comes
    # from the node's own database through `core`/`team`/`pinned` above.
    # The front-Studio (this voice line) vs back-office (CLI agent) split -- what
    # an agent does with its own hands and what it delegates -- belongs in a
    # per-channel prompt in the DB, editable from the panel; it is a constant here
    # only until that lands.
    prompt = f"""{opening}

You are on a phone. Answers are 1-3 sentences, spoken like a person, never like a
system. Never mention instructions, rules, tools or "the system". Speak
{lang_name} for this whole call and do not drift to another language.

You are one person. The functions you were given here are what you can do while
talking. The same you, at your desk between calls, has the rest -- mail,
documents, memory. That desk is not a colleague and not a system: it is you.
Never speak of yourself in the third person.

Two rules, and they matter more than anything else here:
- If you say you will do something, DO IT IN THE SAME TURN, with a function.
  Saying it and calling nothing is a lie, and it is the worst thing you can do on
  this line.
- Never call a thing done until you have read the result. In hand is not done.

Now: {now_str}. This is the only correct time.
"""

    # Caller's reachable email (from their verified identity), so the agent emails
    # them without asking for the address.
    _caller_email = (user or {}).get("caller_email")
    if _caller_email:
        prompt += (
            f"\nThe caller's email address is {_caller_email}. When they ask you to email "
            f"them something, use THIS address in your delegation -- do not ask them for it. Know it SILENTLY: never read the email address (or any personal contact detail) out loud, and never put it in a spoken confirmation, unless the caller explicitly asks you to read it back to confirm it.\n")

    display_name = name or profile.get("name", "")
    if not display_name:
        prompt += """
IMPORTANT: This is a NEW verified user. Introduce yourself briefly and ask their name.
Keep it warm and natural.
"""
    else:
        _live = bool(window_get(wkey)) if wkey else False
        if _live:
            prompt += f"""
You are RESUMING an ongoing live conversation with {display_name} (the recent transcript is below). Continue from where it left off. Do NOT greet again, do NOT re-introduce yourself, do NOT start over -- just pick up the thread.
"""
        else:
            prompt += f"""
This is a RETURNING user named {display_name}.
Greet them briefly by name. Do NOT re-introduce yourself. Do NOT list your capabilities.
"""

    # Profile
    if profile:
        parts = []
        for k, v in profile.items():
            if k == "name":
                continue
            if isinstance(v, list):
                parts.append(f"  {k}: {', '.join(str(x) for x in v)}")
            elif isinstance(v, dict):
                parts.append(f"  {k}: {', '.join(f'{sk}: {sv}' for sk, sv in v.items())}")
            else:
                parts.append(f"  {k}: {v}")
        if parts:
            prompt += "\n== What you know about this user ==\n" + "\n".join(parts) + "\n"

    # Queue entries
    queue = memory.get("queue", [])
    active_q = [e for e in queue if e.get("status") == "active"]
    learns = [e for e in active_q if e.get("subtype") == "learn"]
    reminds = [e for e in active_q if e.get("subtype") == "remind"]
    summaries = [e for e in active_q if e.get("subtype") == "system" and e.get("category") == "conversation_summary"]
    # Hide internal categories from prompt rendering (capability_request is outbound, not for user)
    HIDDEN_CATS = {"conversation_summary", "capability_request"}
    other_sys = [e for e in active_q if e.get("subtype") == "system" and e.get("category") not in HIDDEN_CATS]

    if learns:
        prompt += "\n== Things to remember ==\n"
        by_cat = {}
        for e in learns:
            by_cat.setdefault(e.get("category", "general"), []).append(e)
        for cat, entries in by_cat.items():
            label = cat.replace("_", " ").title()
            prompt += f"  [{label}]\n"
            for e in entries:
                prompt += f"    - {e['text']}\n"

    active_reminds = [e for e in reminds if not e.get("_missed")]
    missed_reminds = [e for e in reminds if e.get("_missed")]

    if active_reminds:
        prompt += "\n== Active reminders ==\n"
        for e in active_reminds:
            exp = f" (until {e['expires'][:10]})" if e.get('expires') else ''
            prompt += f"  - {e['text']}{exp}\n"

    if missed_reminds:
        prompt += "\n== Missed reminders (already past, do NOT present as upcoming) ==\n"
        for e in missed_reminds:
            prompt += f"  - MISSED: {e['text']} (was scheduled for {e.get('expires', 'unknown')[:16]})\n"

    if other_sys:
        prompt += "\n== System notices ==\n"
        for e in other_sys:
            prompt += f"  - {e['text']}\n"

    # Sliding window transcript (raw recent conversation)
    window_text = window_get(wkey) if wkey else ""
    if window_text:
        prompt += f"\n== Recent conversation (live transcript) ==\n{window_text}\n"

    # Conversation summaries from queue (only if no window)
    if summaries and not window_text:
        prompt += "\n== Recent conversation history ==\n"
        for e in summaries[-3:]:
            date_str = e.get('created', '')[:16].replace('T', ' ')
            prompt += f"  - [{date_str}] {e['text']}\n"

    # Instructions
    if profile or active_q or window_text:
        prompt += """
Use what you know about this caller naturally; do not recite it. Do not repeat
anything already said in this conversation. Say a reminder once, at the start.
When they say goodbye, say one short farewell and call end_call.
"""
    return prompt


# --- Conversation memory (in-session) ---
conversation_history: dict[str, list[str]] = {}

# Track pending extractions per uid
pending_extractions: dict[str, asyncio.Event] = {}

# In-flight capability gap requests waiting for user confirmation (per uid)
# Structure: {uid: {"text": str, "session_id": str, "ts": float}}
pending_forwards: dict[str, dict] = {}

# Telegram bridge config (brin-client-tg /send endpoint via Traefik VPN entrypoint)
FORWARD_BRIDGE_URL = os.environ.get("VOICE_FORWARD_URL", "https://tg.local/send")
FORWARD_BRIDGE_ENABLED = os.environ.get("VOICE_FORWARD_ENABLED", "1") == "1"
FORWARD_BRIDGE_VERIFY_TLS = os.environ.get("VOICE_FORWARD_VERIFY_TLS", "0") == "1"

# brin (the node dispatcher). voice forwards real actions HERE
# instead of the legacy Telegram bridge, so the voice agent delegates/acts on
# the shared bus (channel=voice) exactly like the telegram and web-chat
# channels. Internal-only: reached via the docker-host gateway, ufw-opened.
BRIN_URL = os.environ.get("BRIN_URL", "").rstrip("/")
BRIN_API_KEY = os.environ.get("BRIN_API_KEY", "")

# cross-channel memory. At session start the voice agent is given
# the person's recent turns from their OTHER channels (telegram/web) via the
# gateway, so it continues the same conversation. For the single-user demo the
# conversation ids are configured here; S3 will resolve them per-uid from
# user_channels instead of a static list.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")


# --- Budgets --------------------------------------------------------
# Voice talks to its provider directly, so the gateway never sees this spend and
# no budget ever counted a conversation. Two calls close that: ask before the
# call starts, report when it ends. Neither is allowed to break a call -- a
# person is on the line -- but a failure is loud in the log rather than silent.


async def spend_allowed(agent: str) -> tuple[bool, str]:
    """May this call start? Answered by the same budgets the chat path obeys.

    Fails OPEN: if the gateway cannot be reached, the call proceeds. Refusing a
    person because an internal service is down is worse than one unbudgeted
    conversation, and the spend is still reported afterwards.
    """
    if not GATEWAY_URL:
        return True, ""
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"{GATEWAY_URL}/api/v1/spend/allowance",
                            params={"agent": agent or ""})
            if r.status_code != 200:
                print(f"[Budget] allowance check returned {r.status_code}; letting the call through")
                return True, ""
            body = r.json()
            return bool(body.get("allowed", True)), str(body.get("reason") or "")
    except Exception as e:
        print(f"[Budget] allowance check failed ({e}); letting the call through")
        return True, ""


async def report_spend(agent: str, session_id, stats: dict) -> None:
    """Put what this call cost onto the node's budgets.

    The number is an ESTIMATE: voice prices its own audio from byte counts, not
    from provider usage metadata, so it is labelled as such all the way into the
    budget row. An estimate is worth more than a silence and less than a
    measurement, and the label is how the next reader can tell.
    """
    cost = float(stats.get("cost") or 0)
    if cost <= 0 or not GATEWAY_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{GATEWAY_URL}/api/v1/spend/external", json={
                "agent": agent or None,
                "cost_usd": cost,
                "source": "voice",
                "estimated": True,
                "detail": {"session_id": str(session_id),
                           "duration_s": stats.get("duration"),
                           "turns": stats.get("turns")},
            })
            if r.status_code >= 300:
                print(f"[Budget] spend report refused {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Budget] spend report failed ({e}); this call is missing from the node total")
CROSSCHANNEL_CONVERSATIONS = [c.strip() for c in os.environ.get("VOICE_CROSSCHANNEL_CONVERSATIONS", "").split(",") if c.strip()]
CROSSCHANNEL_TURNS = int(os.environ.get("VOICE_CROSSCHANNEL_TURNS", "8"))


async def parse_capability_tags(uid: str, session_id: str, text: str, wkey: str = "") -> None:
    """Detect [CAPABILITY_GAP:...] / [FORWARD:CONFIRMED] / [FORWARD:DECLINED] markers
    in the given text (from thinking OR transcript out -- Gemini Native Audio sometimes
    leaks tags into the spoken response). Triggers delivery on confirmation."""
    upper = text.upper()

    # CAPABILITY_GAP -- substring up to closing ] or end of string
    gap_marker = "[CAPABILITY_GAP:"
    gap_idx = upper.find(gap_marker)
    if gap_idx >= 0:
        start = gap_idx + len(gap_marker)
        end = text.find("]", start)
        if end == -1:
            end = len(text)
        gap_text = text[start:end].strip().rstrip(".,!? ")
        if gap_text and len(gap_text) > 3:
            pending_forwards[uid] = {
                "text": gap_text,
                "session_id": session_id,
                "ts": time.time(),
            }
            print(f"[Session {session_id}] CAPABILITY_GAP pending: {gap_text[:80]}")

    if "[FORWARD:CONFIRMED" in upper or "[FORWARD: CONFIRMED" in upper:
        pending = pending_forwards.pop(uid, None)
        if pending:
            snippet_lines = [f"{spk}: {txt}" for _, spk, txt in transcript_window.get(wkey or uid, [])[-8:]]
            asyncio.create_task(create_capability_request(uid, pending["text"], snippet_lines))
            await log_message(session_id, "server", "capability_request_sent", text=pending["text"][:200])
            print(f"[Session {session_id}] CAPABILITY_GAP confirmed and queued for delivery")
        else:
            print(f"[Session {session_id}] FORWARD:CONFIRMED but no pending request")

    if "[FORWARD:DECLINED" in upper or "[FORWARD: DECLINED" in upper:
        pending_forwards.pop(uid, None)
        print(f"[Session {session_id}] FORWARD declined")


def strip_control_tags(text: str) -> str:
    """Remove our internal control markers from text before storing in user-visible transcript."""
    import re as _re
    text = _re.sub(r"<ctrl[^>]*>", "", text)  # Gemini Live control tokens (<ctrl46>) that leak into transcription
    if "[" not in text:
        return text
    out = text
    for marker in ("[CAPABILITY_GAP:", "[FORWARD:CONFIRMED", "[FORWARD: CONFIRMED",
                   "[FORWARD:DECLINED", "[FORWARD: DECLINED"):
        while True:
            up = out.upper()
            idx = up.find(marker)
            if idx == -1:
                break
            end = out.find("]", idx)
            if end == -1:
                # Unclosed -- drop from idx to end of string
                out = out[:idx].rstrip()
                break
            out = (out[:idx] + out[end + 1:]).strip()
    return out


async def fetch_crosschannel_context(identity: str, agent: str) -> str:
    """The person's recent conversation across EVERY channel, from the unified
    registry: core-api `/api/messages/by-contact` resolves the caller to
    ONE contact and returns their history over telegram, email, the web chat and
    voice alike, as far as `agent` (the one on this call) was part of it. Returns
    a prompt block, or '' when the caller resolves to no contact or there is
    nothing. Best-effort; never raises.

    Asked AS the agent, with the same service proof as every other call voice
    makes for it. by-contact used to answer anyone about anyone, so the
    agent on the line was read the caller's conversations with its colleagues.

    Supersedes the old static-list mechanism, which read a STATIC list of
    conversation ids (VOICE_CROSSCHANNEL_CONVERSATIONS) out of the gateway store
    rather than the registry, and was unconfigured on most nodes -- so the voice
    line saw no cross-channel history at all. The identity is the authenticated
    caller (Authum Remote-User); by-contact resolves it through the one handle
    map + accounts, the same read side the Studio desk chat uses."""
    ident = (identity or "").strip()
    if not ident:
        return ""
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(
                "%s/api/messages/by-contact" % CORE_API_URL,
                params={"identity": ident, "limit": 30},
                headers=_svc_headers(agent))
        if r.status_code != 200:
            # Said, not swallowed: a refusal here reads to the agent as "we have
            # never spoken", the exact mistake this block exists to prevent.
            print("[crosschannel] by-contact HTTP %s for agent %r" % (r.status_code, agent))
            return ""
        data = r.json() or {}
    except Exception as e:
        print("[crosschannel] fetch failed: %s" % e)
        return ""
    msgs = data.get("messages") or []
    if not msgs:
        return ""
    # CRITICAL: drop the agent's OWN spoken (voice-outbound) replies. That is where
    # its past mistakes live -- "we did not talk this morning", "I have no access" --
    # and feeding them back makes it repeat them, confirming its own error every
    # call (the earlier change "recalled text beats the instruction" trap, seen live: the
    # owner called three mornings running and each time it insisted they had not
    # spoken). What the CALLER said (inbound, any channel) is ground truth for what
    # was discussed; other channels' outbound is real content and is kept.
    _deny = ("no access", "cannot see", "did not talk", "didn't talk")
    lines = []
    for m in msgs:
        content = (m.get("content") or "").strip().replace(chr(10), " ")
        if not content:
            continue
        ch = m.get("channel_type") or "?"
        direction = m.get("direction")
        low = content.lower()
        if direction == "outbound" and (ch == "voice" or any(d in low for d in _deny)):
            continue
        role = "They said" if direction == "inbound" else "You replied"
        lines.append("[%s] %s: %s" % (ch, role, content[:200]))
    if not lines:
        return ""
    block = chr(10).join(lines)
    return ("\n== GROUND TRUTH -- your recent history with THIS caller across their "
            "channels (voice, telegram, web chat, email), oldest first. This is what "
            "was REALLY said; trust it over your own memory. If they ask whether you "
            "spoke, when, or about what -- on any channel or this morning -- the answer "
            "is drawn ONLY from the lines below: yes you spoke, and here is about what. "
            "NEVER tell them you did not talk or cannot see their messages when lines "
            "appear here -- they are in front of you. Do not volunteer this unprompted "
            "or recap it when greeting. ==\n" + block + "\n")


CORE_API_URL = (os.environ.get("SN_CORE_API_URL") or "").rstrip("/")
if not CORE_API_URL:
    raise RuntimeError(
        "SN_CORE_API_URL is not set. "
        "The node installer should write this to the service environment."
    )

# What this service presents when it acts for an agent.
#
# The line's whole job is to let a person speak to an agent, which means
# originating messages AS that agent -- and voice is a container, so it can never
# be the agent's own process and can never be named by the kernel. It therefore
# proves ITSELF with a shared secret and names the agent separately; core-api
# verifies the secret, checks the (service, agent) pair against its own list, and
# records the service on the row so the message can later say a line caused it.
SERVICE_NAME = "voice"
SERVICE_SECRET = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()


def _svc_headers(agent: str) -> dict:
    """Headers for a call made on `agent`'s behalf.

    Remote-User is still sent. Not as a fallback that might work -- over TCP it
    cannot -- but so that a node where the secret was never configured fails
    exactly as it does today, with the same 403 and the same log line, instead of
    failing in a new way that has to be diagnosed from scratch.
    """
    h = {"Remote-User": agent, "Content-Type": "application/json"}
    if SERVICE_SECRET:
        h["X-SN-Service-Auth"] = SERVICE_SECRET
        h["X-SN-Service"] = SERVICE_NAME
        h["X-SN-On-Behalf-Of"] = agent
    return h


def _owner_read_headers() -> dict:
    """Headers for the reads voice makes of the whole node: its agents, their
    tools and channels, the event log.

    They are read as the node's owner, by the name this node was configured
    with. The name used to be written here as a literal, which is the owner
    only on a node whose owner happens to be called that; anywhere else every
    one of these reads was refused. No default: a guessed owner would be an
    identity nobody set.
    """
    owner = os.environ.get("SN_OPERATOR_ACTOR", "").strip()
    if not owner:
        raise RuntimeError(
            "SN_OPERATOR_ACTOR is not set in the voice environment: voice cannot "
            "read the node as its owner (re-run the core-api and voice companions)")
    return {"Remote-User": owner}


def _origin_headers() -> dict:
    """Headers for the calls voice makes to core-api as ITSELF.

    Recording a call's turns, reporting that a call ended and writing an event
    act for no agent, so no agent is named: core-api admits them because a
    listed node service proved itself. There is no bare form of these calls; a
    missing credential is reported here by name instead of being sent and read
    back as an unexplained 403.
    """
    if not SERVICE_SECRET:
        raise RuntimeError(
            "SN_SERVICE_AUTH_SECRET is not set in the voice environment: voice "
            "cannot prove its callbacks to core-api (re-run the core-api and "
            "voice companions)")
    return {"X-SN-Service": SERVICE_NAME, "X-SN-Service-Auth": SERVICE_SECRET}
# Studio serves the avatar static store; voice-ui proxies it internally (no auth on
# the bridge) so the phone shows the same agent photos the Studio UI does.
STUDIO_URL = os.environ.get("SN_STUDIO_URL", "").rstrip("/")


async def emit_event(actor: str, event_type: str, entity_type: str = "call",
                     entity_id: str = "", payload: dict | None = None) -> None:
    """Write one row to the node's event log.

    A call used to leave NOTHING here. Diagnosing the call of 2026-08-06 13:59
    meant reading `sn_core.messages` row by row and correlating by timestamp,
    because the audit surface an operator reaches for first was empty for the
    entire window that contained the call. Every other component on the
    node emits; the line was the one blind channel, which is also why nothing
    about it could ever be proven or disproven.

    Best-effort and never raises: an event that cannot be written must not end a
    call. Failures print, because a silent emitter would be the very defect this
    exists to remove."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            await c.post(f"{CORE_API_URL}/api/events", headers=_origin_headers(), json={
                "actor": actor or "component:voice",
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": (entity_id or "")[:200],
                "payload": payload or {},
            })
    except Exception as e:
        print(f"[events] {event_type} not recorded: {e}")


def _valid_slug(s: str) -> bool:
    s = str(s or "")
    return bool(s) and len(s) <= 41 and s[:1].isalpha() and s.replace("-", "").isalnum() and s == s.lower()


async def _agent_voice_enabled(client: httpx.AsyncClient, slug: str) -> bool:
    """True if the agent has the `voice` channel enabled (source of truth: core-api)."""
    if not _valid_slug(slug):
        return False
    try:
        r = await client.get(f"{CORE_API_URL}/api/agents/{slug}/channels",
                             headers=_owner_read_headers())
        if r.status_code != 200:
            return False
        for ch in (r.json().get("channels") or []):
            if ch.get("channel_slug") == "voice" and ch.get("enabled"):
                return True
    except Exception:
        pass
    return False


_DEFAULT_AGENT_CACHE: dict = {}


async def default_agent(client: httpx.AsyncClient | None = None) -> str:
    """Which agent this line serves when the caller has not picked one.

    Resolved from the node, never named here: the oldest enabled agent that has
    the voice channel, else the oldest enabled agent at all. On a fresh node that
    is the base agent the installer created; after the owner has built a team it
    is whoever they created first. This file must not assume a slug -- a node
    names its own agents, and the one this code used to fall back to did not
    exist on most of them, so every unpicked call addressed a stranger.

    Returns "" when the node has no agent to serve. The caller must then refuse
    to delegate rather than send to a name nobody answers.
    """
    if _DEFAULT_AGENT_CACHE.get("slug") is not None:
        return _DEFAULT_AGENT_CACHE["slug"]
    own = client is None
    c = client or httpx.AsyncClient(timeout=5.0)
    slug = ""
    try:
        r = await c.get(f"{CORE_API_URL}/api/agents", headers=_owner_read_headers())
        if r.status_code == 200:
            rows = [a for a in ((r.json() or {}).get("agents") or [])
                    if a.get("enabled") and not a.get("archived_at") and _valid_slug(a.get("slug") or "")]
            rows.sort(key=lambda a: a.get("id") or 0)
            for a in rows:                       # prefer one that actually answers a phone
                if await _agent_voice_enabled(c, a["slug"]):
                    slug = a["slug"]
                    break
            if not slug and rows:
                slug = rows[0]["slug"]
    except Exception as e:
        print("[agent] default lookup failed: %s" % e)
        return ""                                 # do not cache a transient failure
    finally:
        if own:
            await c.aclose()
    _DEFAULT_AGENT_CACHE["slug"] = slug
    if not slug:
        print("[agent] this node has no agent to answer the line")
    return slug


async def resolve_auth_identity(remote_user: str) -> dict | None:
    """Resolve an Authum Remote-User to the registered person (first name / role /
    owner flag) via core-api. Returns None on miss or error so the caller falls
    back to the legacy anonymous onboarding."""
    ident = (remote_user or "").strip()
    if not ident:
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"{CORE_API_URL}/api/users/resolve-identity",
                            params={"identity": ident})
        if r.status_code == 200:
            d = r.json()
            if d.get("found"):
                full = (d.get("name") or "").strip()
                first = full.split()[0] if full else ident
                return {"name": first, "full_name": full,
                        "role": d.get("role"), "is_owner": d.get("is_owner"),
                        "email": (d.get("email") or "").strip()}
    except Exception as e:
        print(f"[auth] resolve failed: {e}")
    return None


# Phrases that assert an action, in either tense. The past tense ("I sent it")
# was the original catch: a claim that the thing is already done. The FUTURE and
# PRESENT are just as much a lie when no tool follows -- "I will send it", "I am
# looking into it", said and then nothing called, is the failure a caller
# actually meets, and it went unguarded because the list held only past forms.
# Bare fillers ("one moment") are deliberately NOT here: they are said honestly
# right before a real call, and the guard has no way to know which is coming.
_ACTION_CLAIM_MARKERS = (
    # Russian -- done
    "\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u043b",        # otpravil
    "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d",  # otpravlen
    "\u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0439",        # proveryay
    "\u043f\u0440\u0438\u0448\u043b\u043e",                      # prishlo
    # Russian -- promised
    "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044e",        # otpravlyu
    "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u044e",  # otpravlyayu
    "\u043d\u0430\u043f\u0438\u0448\u0443",                      # napishu
    "\u0441\u0434\u0435\u043b\u0430\u044e",                      # sdelayu
    "\u043f\u0440\u043e\u0432\u0435\u0440\u044e",               # proveryu
    "\u0443\u0437\u043d\u0430\u044e",                             # uznayu
    "\u0443\u0442\u043e\u0447\u043d\u044e",                      # utochnyu
    "\u0437\u0430\u043f\u0440\u043e\u0448\u0443",               # zaproshu
    "\u043f\u0435\u0440\u0435\u0434\u0430\u043c",               # peredam
    "\u0437\u0430\u0439\u043c\u0443\u0441\u044c",               # zaymus
    # Ukrainian -- promised
    "\u0432\u0456\u0434\u043f\u0440\u0430\u0432\u043b\u044e",  # vidpravlyu
    "\u0437\u0440\u043e\u0431\u043b\u044e",                      # zroblyu
    "\u043f\u0435\u0440\u0435\u0432\u0456\u0440\u044e",        # perevirju
    # English -- done
    "i sent", "i've sent", "i have sent", "sent it", "message sent",
    "email sent", "delivered",
    # English -- promised
    "i'll send", "i will send", "i'm sending", "i am sending",
    "i'll write", "i will write", "i'll check", "i will check",
    "i'll find out", "i'll ask", "i will ask", "let me send", "let me check",
)


def _claims_action(text):
    """True if the spoken text claims a send/delivery happened -- used to catch the
    model claiming success without actually having called ask_brin."""
    t = (text or "").lower()
    return any(m in t for m in _ACTION_CLAIM_MARKERS)


_GUARD_MSG = (
    # Prescribes the RIGHT tool, not one tool. Written when send_message was the
    # only door, this said "call send_message NOW" -- so a caller asking "any
    # news?" got a work order filed instead of a mailbox read, which is worse
    # than the silence it was meant to fix.
    "INTERNAL SYSTEM CHECK (do NOT read this aloud; it is not from the caller): "
    "you just said you would do or check something, but you called NO tool this "
    "turn, so nothing happened. Words are not the work. CALL send_message NOW, to "
    "'self' (that is you at your desk) or to the colleague whose domain it is, "
    "with the context of this conversation -- unless what the caller wanted was "
    "simply to be told what is in your mail, in which case read it instead. Then "
    "tell them plainly what you did -- never that it is finished when it is not."
)


async def record_voice_to_bus(identity: str, transcript: list, session_id: str | None = None, agent: str = "") -> None:
    """End-of-call RECONCILIATION: write any turns the live per-turn
    writes missed. Idempotent -- each turn is keyed voice:<session>:<seq> by its
    index in the transcript, the SAME key the live writes use -- so a turn already
    written is a no-op and nothing is doubled. Best-effort; never raises.

    (Voice used to record ONLY here, one batch at hangup; a crash before the end
    lost the whole call. Turns are now written as they happen; this stays as the
    backstop that fills any that failed their live write.)"""
    if not identity or not transcript or not session_id:
        return
    turns = []
    for seq, entry in enumerate(transcript):
        label, _, text = (entry or "").partition(":")
        text = text.strip()
        if not text:
            continue
        role = "user" if label.strip().upper() == "USER" else "assistant"
        turns.append({"seq": seq, "role": role, "text": text})
    if not turns:
        return
    agent = (agent or "").strip() or await default_agent()
    if not agent:
        print("[voice-bus] not recorded: no agent to file this call under")
        return
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.post(f"{CORE_API_URL}/api/messages/record-voice-turns",
                             headers=_origin_headers(),
                             json={"identity": identity, "session_id": session_id,
                                   "turns": turns, "agent": agent})
            _got = 0
            _why = ""
            try:
                _b = r.json() or {}
                _got = int(_b.get("recorded") or 0)
                _why = str(_b.get("reason") or "")
            except Exception:
                pass
            # NEW turns filled by the reconcile; 0 is the healthy case when the
            # live writes already stored everything. A non-200 or a resolution
            # failure (reason) is the thing to see.
            print(f"[voice-bus] end reconcile: {_got} new of {len(turns)} turns, "
                  f"HTTP {r.status_code}{': ' + _why if _why else ''}")
            await emit_event("agent:%s" % agent,
                             "voice.transcript.recorded" if r.status_code == 200 else "voice.transcript.dropped",
                             "call", session_id or "",
                             {"identity": identity, "turns": len(turns), "recorded": _got,
                              "http": r.status_code, "reason": _why})
    except Exception as e:
        print(f"[voice-bus] record failed: {e}")


async def record_voice_turn_live(identity: str, session_id: str, agent: str,
                                 seq: int, role: str, text: str) -> None:
    """Write ONE voice turn to the registry the moment it completes: a
    crash then keeps the call, each turn carries its real time, and the current
    call is searchable as it accumulates. Idempotent (external_id
    voice:<session>:<seq>, reconciled at call end); best-effort with one retry;
    never raises and never blocks the call."""
    if not identity or not session_id or not (text or "").strip():
        return
    payload = {"identity": identity, "session_id": session_id, "agent": agent or "",
               "turns": [{"seq": seq, "role": role, "text": text}]}
    why = ""
    for _ in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(f"{CORE_API_URL}/api/messages/record-voice-turns",
                                 headers=_origin_headers(), json=payload)
            if r.status_code == 200:
                return
            why = "HTTP %s" % r.status_code
        except Exception as e:
            why = str(e)[:200]
    print("[voice-bus] live turn not recorded (session=%s seq=%s): %s"
          % ((session_id or "")[:8], seq, why))


async def forward_to_brin(uid: str, user_name: str, request_text: str, context_snippet: str = "",
                          agent: str = "") -> bool:
    """Hand a request to this line's node agent via brin, on the shared bus
    (channel=voice). That agent runs with its real tools -- it can delegate to a
    colleague, create a task, send internal mail -- exactly as in telegram/web.
    Replaces the legacy 'forward to the developer via Telegram' path.
    Returns True on success."""
    if not BRIN_URL or not BRIN_API_KEY:
        print("[brin] BRIN_URL/BRIN_API_KEY not set -- cannot forward")
        return False
    agent = (agent or "").strip() or await default_agent()
    if not agent:
        print("[brin] no agent on this node -- not forwarding")
        return False
    msg = request_text
    if context_snippet:
        msg = ("[Voice call from the owner] The caller said: " + request_text + "\n\n"
               "Recent voice conversation for context:\n" + context_snippet + "\n\n"
               "Act on it now. If you or a colleague can do it (send a message, look "
               "something up, delegate to the right person), DO it and state what you did. "
               "Do not merely offer to pass it on.")
    payload = {
        "message": msg,
        "agent": agent,
        "channel": "voice",
        "context": {"user_id": "voice:%s" % uid},
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("%s/api/message" % BRIN_URL, json=payload,
                                     headers={"X-API-Key": BRIN_API_KEY})
        if resp.status_code == 200:
            reply = (resp.json() or {}).get("content", "")
            print("[brin] voice:%s -> %s: %s | reply: %s" % (uid[:8], agent, request_text[:60], reply[:80]))
            return True
        print("[brin] /api/message -> %s: %s" % (resp.status_code, resp.text[:200]))
        return False
    except Exception as e:
        print("[brin] forward failed: %s" % e)
        return False


async def deliver_capability_request(uid: str, user_name: str, request_text: str, context_snippet: str) -> bool:
    """Hand a request the voice agent wants done to the node. PRIMARY path
   : the node agent via brin, on the shared bus. The legacy Telegram
    bridge is used only when brin is not configured."""
    if BRIN_URL and BRIN_API_KEY:
        return await forward_to_brin(uid, user_name, request_text, context_snippet)
    if not FORWARD_BRIDGE_ENABLED:
        print(f"[Forward] Disabled, would have sent: {request_text[:80]}")
        return False
    msg = (
        f"[Voice feedback]\n"
        f"User: {user_name or 'unknown'} ({uid[:8]})\n"
        f"Request: {request_text}\n"
        f"\nContext:\n{context_snippet}"
    )
    try:
        async with httpx.AsyncClient(timeout=5, verify=FORWARD_BRIDGE_VERIFY_TLS) as client:
            resp = await client.post(FORWARD_BRIDGE_URL, json={"text": msg, "source": "voice"})
            if resp.status_code == 200:
                print(f"[Forward] Sent capability_request from {uid[:8]}: {request_text[:80]}")
                return True
            print(f"[Forward] Bridge returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"[Forward] Failed to deliver: {e}")
        return False


async def create_capability_request(uid: str, request_text: str, transcript_lines: list) -> None:
    """Create capability_request queue entry, deliver via Telegram, and on success
    create capability_request_ack for next session."""
    memory = await get_user_memory(uid)
    user = await get_or_create_user(uid)
    queue = memory.get("queue", [])
    now_iso = datetime.now(timezone.utc).isoformat()

    # Dedup: skip if same request text exists in last 24h
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    norm = request_text.strip().lower()
    for e in queue:
        if e.get("subtype") == "system" and e.get("category") == "capability_request":
            try:
                created = datetime.fromisoformat(e.get("created", "").replace("Z", "+00:00"))
                if created > cutoff_dt and e.get("text", "").strip().lower() == norm:
                    print(f"[Forward] Dedup hit for uid={uid[:8]}: {request_text[:60]}")
                    return
            except Exception:
                pass

    context_snippet = "\n".join(transcript_lines[-8:]) if transcript_lines else "(no context)"

    entry_id = str(uuid.uuid4())
    queue.append({
        "id": entry_id,
        "direction": "remember",
        "subtype": "system",
        "category": "capability_request",
        "text": request_text,
        "context_snippet": context_snippet,
        "created": now_iso,
        "expires": None,
        "max_shows": None,
        "shown_count": 0,
        "status": "active",
        "delivery_status": "pending",
    })
    memory["queue"] = queue
    await save_user_memory(uid, memory)

    delivered = await deliver_capability_request(uid, user.get("name", ""), request_text, context_snippet)

    # Reload + update delivery status (avoid race with concurrent saves)
    memory = await get_user_memory(uid)
    queue = memory.get("queue", [])
    for e in queue:
        if e.get("id") == entry_id:
            e["delivery_status"] = "sent" if delivered else "failed"
            break

    if delivered:
        # Acknowledgment for next session, shown once
        queue.append({
            "id": str(uuid.uuid4()),
            "direction": "notify",
            "subtype": "system",
            "category": "capability_request_ack",
            "text": f"In a previous conversation you asked: {request_text}. I forwarded it to the developer.",
            "created": now_iso,
            "expires": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "max_shows": 1,
            "shown_count": 0,
            "status": "active",
        })

    memory["queue"] = queue
    await save_user_memory(uid, memory)

# --- Sliding window transcript ---
TRANSCRIPT_DIR = Path(os.environ.get("TRANSCRIPT_DIR", "/data/transcripts"))
WINDOW_MAX_TOKENS = 1000  # a call's thread, not a history: enough to resume
                          # after a drop, small enough not to outweigh the rules
DUMP_INTERVAL = 60  # seconds

# In-memory buffer: uid -> [(timestamp, speaker, text), ...]
transcript_window: dict[str, list[tuple[float, str, str]]] = {}

# Syllable buffer for input transcription (comes in fragments)
_syllable_buf: dict[str, tuple[float, str]] = {}  # uid -> (last_time, accumulated_text)


def window_add(uid: str, speaker: str, text: str):
    """Add entry to sliding window."""
    transcript_window.setdefault(uid, [])
    transcript_window[uid].append((time.time(), speaker, text))


def window_flush_syllables(uid: str, force: bool = False):
    """Flush accumulated syllables into window as one entry."""
    if uid not in _syllable_buf:
        return
    last_t, accumulated = _syllable_buf[uid]
    if not accumulated.strip():
        return
    # Flush if forced or if enough accumulated
    if force or len(accumulated) > 10:
        window_add(uid, "USER", accumulated.strip())
        del _syllable_buf[uid]


def window_add_syllable(uid: str, fragment: str):
    """Buffer incoming syllables, flush when sentence likely complete."""
    now = time.time()
    if uid in _syllable_buf:
        last_t, accumulated = _syllable_buf[uid]
        # If gap > 2 sec, flush previous and start new
        if now - last_t > 2.0 and accumulated.strip():
            window_add(uid, "USER", accumulated.strip())
            _syllable_buf[uid] = (now, fragment)
        else:
            _syllable_buf[uid] = (now, accumulated + fragment)
    else:
        _syllable_buf[uid] = (now, fragment)


def window_get(uid: str, max_tokens: int = WINDOW_MAX_TOKENS) -> str:
    """Get formatted window for system prompt, trimmed to max_tokens."""
    entries = transcript_window.get(uid, [])
    if not entries:
        # Try loading from file
        fpath = TRANSCRIPT_DIR / f"{uid}.json"
        if fpath.exists():
            try:
                with open(fpath) as f:
                    entries = json.load(f)
                transcript_window[uid] = entries
            except Exception:
                pass
    if not entries:
        return ""

    # Resolve timezone for display
    try:
        tz = ZoneInfo(client_tz) if client_tz else timezone.utc
    except Exception:
        tz = timezone.utc

    # Merge consecutive fragments from same speaker into one line
    merged = []
    current_speaker = None
    current_text = ""
    current_ts = 0
    for ts, speaker, text in entries:
        if speaker == current_speaker:
            current_text += " " + text
        else:
            if current_speaker and current_text.strip():
                merged.append((current_ts, current_speaker, current_text.strip()))
            current_speaker = speaker
            current_text = text
            current_ts = ts
    if current_speaker and current_text.strip():
        merged.append((current_ts, current_speaker, current_text.strip()))

    # Format from newest, trim to token budget
    lines = []
    token_count = 0
    for ts, speaker, text in reversed(merged):
        t_str = datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")
        line = f"[{t_str}] {speaker}: {text}"
        tokens = len(line) // 4 + 1
        if token_count + tokens > max_tokens:
            break
        lines.append(line)
        token_count += tokens

    if not lines:
        return ""
    lines.reverse()
    return "\n".join(lines)


def window_dump(uid: str):
    """Dump window to file."""
    entries = transcript_window.get(uid, [])
    if not entries:
        return
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    fpath = TRANSCRIPT_DIR / f"{uid}.json"
    try:
        with open(fpath, "w") as f:
            json.dump(entries[-500:], f)  # keep last 500 entries max
    except Exception as e:
        print(f"[Window] Dump failed for {uid[:8]}: {e}")


def window_cleanup(uid: str, max_age_sec: int = 1200):
    """Remove entries older than max_age."""
    entries = transcript_window.get(uid, [])
    if not entries:
        return
    cutoff = time.time() - max_age_sec
    transcript_window[uid] = [(t, s, txt) for t, s, txt in entries if t > cutoff]


def window_clear(uid: str):
    """Start a fresh conversation: drop the sliding window in memory AND on disk,
    so the next turn does not resume the previous thread. The person's durable
    memory (profile, cross-channel history) is untouched -- only this call's
    running transcript is wiped. This is what the `new_conversation` tool calls
    when the caller asks to start over, and it is the escape hatch from a thread
    that has gone wrong (e.g. one where the agent talked itself into a mistaken
    belief and kept repeating it)."""
    transcript_window.pop(uid, None)
    _syllable_buf.pop(uid, None)
    try:
        fpath = TRANSCRIPT_DIR / f"{uid}.json"
        if fpath.exists():
            fpath.unlink()
    except Exception as e:
        print(f"[Window] clear failed for {uid[:8]}: {e}")


async def periodic_dump():
    """Background task: dump all windows to files every DUMP_INTERVAL seconds."""
    while True:
        await asyncio.sleep(DUMP_INTERVAL)
        for uid in list(transcript_window.keys()):
            window_dump(uid)




# --- Admin auth ---
async def verify_admin(authorization: str = Header(default="")):
    """Check admin token for debug endpoints."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin access not configured")
    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")



# --- Rate limiting ---
_session_counts: dict[str, list[float]] = {}  # ip -> [timestamps]

def check_rate_limit(ip: str) -> bool:
    """Return True if allowed, False if rate limited."""
    now = time.time()
    hour_ago = now - 3600
    times = _session_counts.get(ip, [])
    times = [t for t in times if t > hour_ago]
    _session_counts[ip] = times
    if len(times) >= MAX_SESSIONS_PER_HOUR:
        return False
    times.append(now)
    return True

# --- App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await migrate_db()
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    dump_task = asyncio.create_task(periodic_dump())
    yield
    dump_task.cancel()


app = FastAPI(lifespan=lifespan)

from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

app.add_middleware(SecurityHeadersMiddleware)
# ============================================================================
# Mode B: browser ASR -> brin (the brain) -> Gemini TTS.
# Reliable tool use, one brain, no native-audio function-calling. The browser
# transcribes speech (Web Speech) and plays back the TTS audio we return.
# ============================================================================
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-preview-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "Aoede")


async def _tts(text: str, voice: str = None) -> dict:
    """Gemini TTS: text -> PCM audio (base64, 24kHz). Returns {audio, rate} or {}."""
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (TTS_MODEL, key)
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or TTS_VOICE}}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, json=body)
        if r.status_code != 200:
            print("[tts] %s: %s" % (r.status_code, r.text[:160]))
            return {}
        part = r.json()["candidates"][0]["content"]["parts"][0].get("inlineData", {})
        return {"audio": part.get("data", ""), "rate": 24000}
    except Exception as e:
        print("[tts] failed: %s" % e)
        return {}


async def _brin_chat(uid: str, text: str, agent: str = "") -> str:
    """Send the caller's text to brin (this line's agent, channel voice) and
    return the answer. Brin is the brain in mode B -- reliable tool use."""
    brin_url = os.environ.get("BRIN_URL", "").rstrip("/")
    brin_key = os.environ.get("BRIN_API_KEY", "")
    if not brin_url or not brin_key:
        return "Voice is not connected to the node brain right now."
    agent = (agent or "").strip() or await default_agent()
    if not agent:
        return "There is nobody set up on this node to take that yet."
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(brin_url + "/api/message",
                             headers={"X-API-Key": brin_key},
                             json={"message": text, "agent": agent, "channel": "voice",
                                   "context": {"user_id": "voice:%s" % uid}, "stream": False})
        if r.status_code != 200:
            print("[brin] %s: %s" % (r.status_code, r.text[:160]))
            return "Something is not working on my side right now, but the team already knows and will sort it out soon."
        return (r.json() or {}).get("content") or "Sorry, I did not catch that. Could you say it again?"
    except Exception as e:
        print("[brin] failed: %s" % e)
        return "Something went wrong on my side, but the team already knows and will fix it soon."


def _chunks(text):
    """Split an answer into sentence-ish pieces so the first audio plays fast."""
    import re as _re
    parts = [c.strip() for c in _re.split(r"(?<=[.!?\u2026])\s+|\n+", text or "") if c.strip()]
    if parts:
        return parts
    t = (text or "").strip()
    return [t] if t else []


async def _speak(ws, text):
    """TTS the text one sentence at a time, streaming each clip to the client so
    the voice starts within ~1-2s instead of waiting for the whole answer."""
    for ch in _chunks(text):
        au = await _tts(ch)
        if au.get("audio"):
            await ws.send_json({"type": "tts_audio", "audio": au["audio"], "rate": au["rate"]})


@app.websocket("/ws-brain")
async def ws_brain(client_ws: WebSocket):
    await client_ws.accept()
    uid = None
    try:
        first = await client_ws.receive_json()
        uid = validate_uid(first.get("uid", str(uuid.uuid4())))
        await get_or_create_user(uid)
        await client_ws.send_json({"type": "status", "text": "ready"})
        _gr = "Hello! How can I help?"
        await client_ws.send_json({"type": "transcript_out", "text": _gr})
        await _speak(client_ws, _gr)
        while True:
            data = await client_ws.receive_json()
            if data.get("type") != "user_text":
                continue
            text = (data.get("text") or "").strip()
            if not text:
                continue
            print("[ws-brain %s] user: %s" % (uid[:8], text[:80]))
            await client_ws.send_json({"type": "thinking"})
            answer = await _brin_chat(uid, text)
            print("[ws-brain %s] agent: %s" % (uid[:8], answer[:80]))
            await client_ws.send_json({"type": "transcript_out", "text": answer})
            await _speak(client_ws, answer)
    except (WebSocketDisconnect, Exception) as e:
        print("[ws-brain] done: %s" % e)


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")



@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/brain")
async def brain_page():
    return FileResponse(str(FRONTEND_DIR / "brain.html"))


@app.get("/debug")
async def debug_page(authorization: str = Header(default="")):
    await verify_admin(authorization)
    return FileResponse(str(FRONTEND_DIR / "debug.html"))


@app.get("/manifest.json")
async def manifest():
    return FileResponse(str(FRONTEND_DIR / "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(str(FRONTEND_DIR / "sw.js"), media_type="application/javascript")


@app.post("/api/register")
async def register_user(request: Request):
    body = await request.json()
    uid = body.get("uid", str(uuid.uuid4()))
    user = await get_or_create_user(uid)
    return JSONResponse(user)


@app.post("/api/update-profile")
async def update_profile(request: Request):
    body = await request.json()
    uid = body.get("uid", "")
    if not uid:
        return JSONResponse({"error": "uid required"}, status_code=400)
    name = body.get("name")
    prefs = body.get("preferences")
    await update_user(uid, name=name, preferences=prefs)
    return JSONResponse({"ok": True})


@app.get("/api/debug")
async def debug_info(authorization: str = Header(default="")):
    await verify_admin(authorization)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        users = [dict(r) for r in await db.execute_fetchall("SELECT * FROM users ORDER BY last_seen DESC LIMIT 50")]
        sessions = [dict(r) for r in await db.execute_fetchall(
            "SELECT s.*, u.name FROM sessions s LEFT JOIN users u ON s.uid=u.uid ORDER BY s.id DESC LIMIT 50"
        )]
        totals = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt, SUM(cost) as total_cost, SUM(duration_s) as total_duration FROM sessions"
        )
        t = dict(totals[0]) if totals else {}
    return JSONResponse({
        "users": users,
        "recent_sessions": sessions,
        "totals": t,
    })


@app.post("/api/flush-window")
async def flush_window(authorization: str = Header(default="")):
    await verify_admin(authorization)
    # No-downtime demo reset: clear the per-process in-memory conversation state so the
    # next call starts clean WITHOUT a container restart (a restart takes the call UI
    # offline). demo-reset.sh calls this instead of `docker restart`.
    nw, nc, npd = len(transcript_window), len(conversation_history), len(pending_extractions)
    transcript_window.clear()
    conversation_history.clear()
    pending_extractions.clear()
    print("[flush-window] cleared windows=%d histories=%d pending=%d" % (nw, nc, npd))
    return JSONResponse({"ok": True, "cleared": {"windows": nw, "histories": nc, "pending": npd}})


@app.get("/api/logs/{session_id}")
async def session_logs(session_id: str, authorization: str = Header(default="")):
    await verify_admin(authorization)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = [dict(r) for r in await db.execute_fetchall(
            "SELECT * FROM logs WHERE session_id=? ORDER BY id", (session_id,)
        )]
    return JSONResponse(rows)


@app.get("/api/memory/{uid}")
async def get_memory(uid: str, authorization: str = Header(default="")):
    await verify_admin(authorization)
    """Debug endpoint: view user memory."""
    memory = await get_user_memory(uid)
    return JSONResponse(memory)



@app.post("/api/memory/{uid}/notify")
async def add_notification(uid: str, request: Request, authorization: str = Header(default="")):
    await verify_admin(authorization)
    body = await request.json()
    memory = await get_user_memory(uid)
    queue = memory.get("queue", [])
    queue.append({
        "id": str(uuid.uuid4()),
        "direction": "notify",
        "subtype": body.get("subtype", "system"),
        "category": body.get("category", "general"),
        "text": body["text"],
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": body.get("expires"),
        "max_shows": body.get("max_shows", 3),
        "shown_count": 0,
        "status": "active"
    })
    memory["queue"] = queue
    await save_user_memory(uid, memory)
    return JSONResponse({"ok": True})


@app.get("/api/agents")
async def voice_agents():
    """The picker's cast: node agents whose `voice` channel is enabled. Pulled live
    from core-api, so enabling voice on an agent (panel/studio) makes it callable here
    with no code change. `avatar` is the agent's data field, served via /avatars."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{CORE_API_URL}/api/agents", headers=_owner_read_headers())
            data = r.json()
            roster = (data.get("agents") if isinstance(data, dict) else data) or []
            flags = await asyncio.gather(*[_agent_voice_enabled(c, a.get("slug", "")) for a in roster])
        out = [{"slug": a["slug"], "title": a.get("title") or a["slug"],
                "role": a.get("role") or a.get("description") or "",
                "avatar": a.get("avatar")}
               for a, on in zip(roster, flags) if on]
        return JSONResponse(out)
    except Exception as e:
        print(f"[voice-agents] failed: {e}")
        return JSONResponse([])


@app.get("/avatars/{path:path}")
async def avatar_proxy(path: str):
    """Serve agent avatar images by proxying the Studio static store internally --
    the same bytes the Studio UI shows (crew + custom). Internal bridge only, no auth."""
    if ".." in path or path.startswith("/") or len(path) > 200:
        raise HTTPException(status_code=400, detail="bad path")
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{STUDIO_URL}/avatars/{path}")
    except Exception:
        raise HTTPException(status_code=502, detail="avatar upstream error")
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="avatar not found")
    ctype = r.headers.get("content-type") or "image/webp"
    if ctype.startswith("text/"):
        ctype = "image/webp"
    return Response(content=r.content, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})

# --- WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(client_ws: WebSocket):
    print("[WS] New connection accepted")
    await client_ws.accept()
    # Rate limit check
    client_ip = (client_ws.headers.get("x-forwarded-for", "").split(",")[0].strip() or client_ws.headers.get("x-real-ip", "") or (client_ws.client.host if client_ws.client else "unknown"))
    if not check_rate_limit(client_ip):
        await client_ws.send_json({"type": "error", "text": "Too many connections. Please wait."})
        await client_ws.close()
        print(f"[WS] Rate limited: {client_ip}")
        return
    gemini_ws = None
    session_id = None
    start_time = time.time()
    cost = {"input_bytes": 0, "output_bytes": 0, "turns": 0}
    uid = None
    session_transcript = []  # bound early so cleanup stats never UnboundLocalError
    _relayed_ids = set()  # answers queued for the caller this call; bound early for the finally block (#9)

    try:
        # Step 1: Wait for init message
        print("[WS] Step 1: waiting for init message...")
        first_msg = await client_ws.receive_json()
        uid = validate_uid(first_msg.get("uid", str(uuid.uuid4())))
        voice = first_msg.get("voice", "Aoede")
        # Persona by host: a dedicated subdomain may serve one named agent -- the
        # first DNS label is read as an agent slug. It counts as a persona door
        # ONLY if the node actually has that agent with voice enabled; an
        # unknown label is just the shared line, because a persona door also
        # skips the access phrase and a name alone must not buy that. Which
        # subdomains exist is decided outside the node; this file only reads the
        # label it was given and asks the node whether such an agent exists.
        _host = (client_ws.headers.get("host") or "").lower()
        _label = _host.split(":")[0].split(".")[0]
        _SHARED_LINE_HOSTS = {"voice", "line", "localhost", ""}
        _is_persona = False
        if _label not in _SHARED_LINE_HOSTS and not _label[:1].isdigit() and _valid_slug(_label):
            async with httpx.AsyncClient(timeout=5.0) as _hc:
                _is_persona = await _agent_voice_enabled(_hc, _label)
        persona_name = _label.capitalize() if _is_persona else None
        # No slug is named here: the shared line serves whoever this node put on
        # the phone (see default_agent).
        brin_agent = _label if _is_persona else await default_agent()
        # In-app picker: the caller chose WHO to call. If that agent has voice enabled
        # it overrides the host default -> one origin, many callable agents.
        # Falls back to the host persona when absent/invalid.
        _sel = str(first_msg.get("agent") or "").strip().lower()
        if _valid_slug(_sel):
            async with httpx.AsyncClient(timeout=5.0) as _vc:
                if await _agent_voice_enabled(_vc, _sel):
                    brin_agent = _sel
                    persona_name = _sel.capitalize()
                    _is_persona = True
        if not brin_agent:
            print("[WS] no agent on this node -- the line answers, but has nobody to hand work to")
        _dname = brin_agent.capitalize() if brin_agent else "Assistant"  # label for events/logs
        # Persona identity from the source of truth (specialist via core-api): the
        # role was invisible in plain chat because only ACTIONS reached brin. Now
        # the voice front itself knows which agent of this business it is.
        (persona_core, persona_pinned, persona_team) = await _fetch_persona_core(brin_agent)  # load persona on EVERY line incl default: front desk = its business (the node's own business), not a generic node assistant
        # Per-session model override (the UI voice/model switcher sends it). Empty
        # or absent -> the env default. Lets the operator audition models live.
        model = (first_msg.get("model") or "").strip() or GEMINI_MODEL
        # This path streams audio both ways (response_modalities=AUDIO); a non-audio model
        # rejects it with close 1007. The browser model picker persists its last pick in
        # localStorage, so a stale text-model value drops every call even if untouched now.
        # Guard server-side with an ALLOWLIST. The old "audio in name" check let the
        # floating -latest alias through (it contains "audio") -- and -latest rotates to
        # a build that rejects the audio config => intermittent 1007. Only pinned
        # native-audio versions verified to stream audio are allowed; anything else
        # (latest, 3.1-flash-live, empty, ...) is forced to the pinned default.
        # 12-2025 consistently 1011s on our live-audio config; 09-2025 is the stable one.
        _AUDIO_OK = {
            "gemini-2.5-flash-native-audio-preview-09-2025",
        }
        if model not in _AUDIO_OK:
            print("[model] %r not in audio allowlist; using %s" % (model, GEMINI_MODEL))
            model = GEMINI_MODEL
        print("[model] session model: %s (client sent: %r)" % (model, first_msg.get("model")))
        client_time = first_msg.get("local_time", "")
        client_tz = first_msg.get("timezone", "")
        client_ua = first_msg.get("ua", "")
        client_screen = f"{first_msg.get('sw', '?')}x{first_msg.get('sh', '?')}"
        client_ip = (client_ws.headers.get("x-forwarded-for", "").split(",")[0].strip() or client_ws.headers.get("x-real-ip", "") or (client_ws.client.host if client_ws.client else "unknown"))
        print(f"[WS] Step 1 OK: uid={uid[:8]}, voice={voice}, tz={client_tz}, ip={client_ip}, screen={client_screen}")

        # Step 2: Get/create user
        # Reconnect stays fast: wait at most 2s for the prior session memory
        # extraction; if it is still running, proceed without blocking (it
        # finishes in the background and applies to the next turn).
        if uid in pending_extractions:
            print(f"[WS] Waiting (max 2s) for pending extraction for {uid[:8]}...")
            try:
                await asyncio.wait_for(pending_extractions[uid].wait(), timeout=2)
            except asyncio.TimeoutError:
                print(f"[WS] Extraction wait timed out for {uid[:8]}")
            print(f"[WS] Extraction done, continuing")

        print(f"[WS] Step 2: get_or_create_user({uid[:8]})...")
        user = await get_or_create_user(uid)
        print(f"[WS] Step 2 OK: name='{user.get('name', '')}', prefs={user.get('preferences', {})}")
        # Authenticated channel (behind Authum): the Remote-User header IS the
        # verified identity. Resolve it to the real person, skip the password gate
        # and name-asking, greet by name. Legacy anonymous onboarding only runs
        # when no Remote-User is present (e.g. unauthenticated internal access).
        _ruser = client_ws.headers.get("remote-user", "").strip()
        if _ruser:
            _ident = await resolve_auth_identity(_ruser)
            if _ident and _ident.get("name"):
                user["name"] = _ident["name"]
                user["verified"] = 1
                # Carry the caller's reachable email into the prompt so the agent can
                # email them without asking for the address (transient, per session).
                if _ident.get("email"):
                    user["caller_email"] = _ident["email"]
                print(f"[WS] auth identity: {_ruser} -> {_ident['name']} (verified, onboarding skipped)")

        # Step 3: Voice preference
        _p = user.get('preferences', {})
        if isinstance(_p, str):
            try:
                _p = json.loads(_p)
            except Exception:
                _p = {}
        if _p.get('voice'):
            voice = _p['voice']
            print(f"[WS] Step 3: using saved voice: {voice}")

        # The call's language, shared by every part of this session. A list rather
        # than two names because it is FILLED IN later, inside _connect_gemini,
        # once the caller's profile has loaded -- and the idle check-in and the
        # message notifications below live in their own nested functions, which
        # would otherwise each hardcode a language the session is not speaking.
        # Until it is resolved it holds the node default, so a mid-setup failure
        # still leaves every instruction agreeing with the speech engine.
        _call_lang = list(_language_by_code(VOICE_LANGUAGE) or ("en-US", "English"))

        # Step 3.5: kick off the Gemini connection NOW so it runs concurrently
        # with the memory load below (the ~3s read overlaps the slow handshake
        # instead of adding to it). Awaited at Step 6.
        _gemini_ws_url = (
            f"wss://generativelanguage.googleapis.com/ws/"
            f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
            f"?key={GOOGLE_API_KEY}"
        )
        async def _connect_gemini():
            _err = "?"
            for _attempt in range(3):
                try:
                    return await asyncio.wait_for(websockets.connect(_gemini_ws_url), timeout=12)
                except Exception as _ge:
                    _err = type(_ge).__name__
                    print(f"[WS] Gemini connect attempt {_attempt + 1}/3 failed: {_err}")
                    if _attempt < 2:
                        await asyncio.sleep(0.6)
            print(f"[WS] Gemini connect FAILED after 3 attempts: {_err}")
            return None
        _gemini_task = asyncio.create_task(_connect_gemini())

        # Step 4: Load memory + sliding window
        # ONE THREAD PER PERSON AND AGENT. The window used to be keyed by caller
        # alone, so switching agent on the same line handed the new persona the
        # previous one's turns as its own history: asked his name, John read back
        # a conversation in which the assistant had repeatedly said it was Alex,
        # and answered "My name is Alex". Calling a colleague you have
        # never called opens an empty thread, as it would with a person.
        # The PROFILE stays keyed by caller -- who you are and what language you
        # speak is about you, not about who you rang.
        wkey = "%s__%s" % (uid, brin_agent or "line")
        print(f"[WS] Step 4: loading memory...")
        memory = await get_user_memory(uid)
        memory = cleanup_memory(memory)
        await save_user_memory(uid, memory)
        window_cleanup(wkey)
        win_text = window_get(wkey)
        win_tokens = len(win_text) // 4 if win_text else 0
        print(f"[WS] Step 4 OK: profile={bool(memory.get('profile'))}, queue={len(memory.get('queue', []))}, window={win_tokens}tok")

        # Step 5: Create session. A budget that stops the chat path has to stop
        # this one too, or "the node is capped" is not true.
        _ok, _why = await spend_allowed(brin_agent)
        if not _ok:
            await client_ws.send_json({"type": "error", "text":
                "The node has reached its spending limit, so I cannot take this "
                "call right now. Raise the limit or wait for the reset."})
            await emit_event("component:voice", "voice.call.refused", "call", uid,
                             {"agent": brin_agent or "", "reason": _why})
            await client_ws.close()
            return
        session_id = await create_session(uid, voice)
        conversation_history.setdefault(session_id, [])
        print(f"[WS] Step 5 OK: session_id={session_id}")

        device_info = f"ip={client_ip} screen={client_screen} ua={client_ua[:80]}"
        await log_message(session_id, "server", "session_start",
                          text=f"uid={uid} voice={voice} name={user.get('name', '')} {device_info}")
        try:
            _caller = client_ws.headers.get("remote-user", "").strip()
        except Exception:
            _caller = ""
        await emit_event("component:voice", "voice.call.started", "call", session_id,
                         {"uid": uid, "caller": _caller, "voice": voice,
                          "name": user.get("name", ""), "ip": client_ip})

        # Step 6: Gemini connection (started at Step 3.5, overlapping the memory load)
        print(f"[WS] Step 6: awaiting Gemini connection...")
        gemini_ws = await _gemini_task
        if gemini_ws is None:
            if session_id:
                await log_message(session_id, "server", "error", text="gemini_connect_failed_3x")
            try:
                await client_ws.send_json({"type": "error",
                    "text": "Could not connect to the voice model (Gemini) after several tries. Please try again."})
            except Exception:
                pass
            return
        print(f"[WS] Step 6 OK: Gemini WebSocket connected")

        # Step 7: Send setup
        print(f"[WS] Step 7: sending setup to Gemini...")
        # The call's language, resolved ONCE: the same value pins the realtime
        # API's language_code below and renders every instruction that mentions a
        # language. Two sources of truth here would mean a prompt telling the model
        # to speak one language while the speech engine is set to another.
        _call_lang[0], _call_lang[1] = resolve_call_language(memory.get("profile") or {})
        print("[WS] call language: %s (%s)" % (_call_lang[1], _call_lang[0]))
        system_prompt = build_system_prompt(user, memory, client_time, client_tz, wkey,
                                             persona_name=persona_name, skip_phrase=_is_persona, core=persona_core, pinned=persona_pinned, team=persona_team,
                                             lang_name=_call_lang[1])
        # Inject the person's cross-channel history (from the unified
        # registry, resolved by the authenticated caller) into the prompt, framed so
        # the agent KNOWS it can see it and answers from it when asked -- the same
        # thing that makes the Studio desk chat recall telegram. The earlier "background
        # only, do not recap" framing made the agent refuse; Gemini also does not
        # reliably CALL the messages_search tool, so an in-prompt block is what
        # actually works here. (messages_search remains for channel-scoped lookups.)
        # As the agent on this call: what it was part of, not the node's.
        system_prompt += await fetch_crosschannel_context(
            _caller or (user or {}).get("caller_email") or uid, brin_agent)
        # B1: standing fact -- time since this caller's PREVIOUS call -- so the model
        # judges greet-vs-continue itself, independent of the one-shot greeting turn and
        # of any async relay injection (which otherwise re-greets the caller mid-call).
        try:
            async with aiosqlite.connect(DB_PATH) as _pdb:
                _prow = await _pdb.execute_fetchall(
                    "SELECT started_at FROM sessions WHERE uid=? AND id<>? ORDER BY started_at DESC LIMIT 1",
                    (uid, session_id))
            if _prow:
                _pp = datetime.fromisoformat(str(_prow[0][0]).replace("Z", "+00:00"))
                if _pp.tzinfo is None:
                    _pp = _pp.replace(tzinfo=timezone.utc)
                _secs = (datetime.now(timezone.utc) - _pp).total_seconds()
                if _secs < 90:      _ph = "less than two minutes ago"
                elif _secs < 3600:  _ph = "%d minute(s) ago" % round(_secs / 60)
                elif _secs < 86400: _ph = "about %d hour(s) ago" % round(_secs / 3600)
                else:               _ph = "about %d day(s) ago" % round(_secs / 86400)
                system_prompt += (
                    chr(10) + chr(10) + "CALL CONTEXT: this caller's previous call ended " + _ph + ". If "
                    "that was only seconds or a couple of minutes ago, you are effectively still in "
                    "the SAME live conversation: do NOT greet again or open with their name when an "
                    "update arrives, just continue. Greet only if it was a while ago.")
            else:
                system_prompt += (chr(10) + chr(10) + "CALL CONTEXT: this is the caller's first call "
                                  "-- greet them warmly once at the start.")
        except Exception as _pe:
            print("[B1 prev-session] %s" % _pe)
        print(f"[WS] Step 7: system prompt length: {len(system_prompt)} chars")

        # Increment shown_count for all active queue entries that were included in prompt
        queue_changed = False
        now_utc = datetime.now(timezone.utc)
        for entry in memory.get("queue", []):
            if entry.get("status") == "active":
                entry["shown_count"] = entry.get("shown_count", 0) + 1
                # Mark done if max_shows reached
                if entry.get("max_shows") is not None and entry["shown_count"] >= entry["max_shows"]:
                    entry["status"] = "done"
                # Set next_show for remind entries: don't show again for 24h
                if entry.get("subtype") == "remind" and not entry.get("_missed"):
                    entry["next_show"] = (now_utc + timedelta(hours=24)).isoformat()
                queue_changed = True
        if queue_changed:
            await save_user_memory(uid, memory)
        # Brin-as-tool: a single function declaration that lets Gemini delegate
        # arbitrary node operations (install companions, list state, run diagnostics,
        # search memory, ...) to Brin. Brin's own tool registry decides what to
        # actually execute. Disabled when BRIN_URL is unset.
        brin_url = os.environ.get("BRIN_URL", "").strip()
        brin_api_key = os.environ.get("BRIN_API_KEY", "").strip()
        brin_tool_decl = None
        if brin_url and brin_api_key:
            brin_tool_decl = [{
                "function_declarations": [{
                    "name": "send_message",
                    "description": (
                        # Opens with the concrete cases on purpose. It used to open with
                        # "put work in hand that you cannot do while on a call", and a caller
                        # saying "send me an email" did not land on that abstraction -- the
                        # model answered "sure, sending" and called nothing, four asks running.
                        "SEND AN EMAIL, write something, look something up, file or create a "
                        "record, check a system -- anything the caller asks for that is not one "
                        "of your other functions here. Call this the moment they ask, before "
                        "you answer them. Set 'to' to 'self': that is you, at your desk, with "
                        "your full toolset -- not a colleague, not another system, so say 'I "
                        "will do it', never 'I asked someone'. Set 'to' to a colleague's name "
                        "from your team list only when the work is genuinely theirs. "
                        "ALWAYS include the relevant conversation context in 'message' (who the "
                        "caller is, what they asked, the specifics) so the recipient can act "
                        "without asking back. The recipient wakes up, does the work, and replies "
                        "to your mail. This only QUEUES the request -- it does NOT mean it is done."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to": {
                                "type": "string",
                                # The colleagues and their domains come from the node DB (the
                                # team roster in the composed persona) -- never name roles here.
                                "description": "Recipient: 'self' for work that is yours -- that is you at your desk, not another person -- or the name of the colleague from your team list who owns that domain."
                            },
                            "message": {
                                "type": "string",
                                "description": "What to do, INCLUDING the relevant conversation context."
                            }
                        },
                        "required": ["to", "message"]
                    }
                }, {
                    "name": "set_voice",
                    "description": (
                        "Change your own speaking voice when the caller asks you to "
                        "switch or change your voice or sound different. Female: Aoede, "
                        "Kore, Leda. Male: Charon, Fenrir, Puck, Orus."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "voice_name": {
                                "type": "string",
                                "description": "One of: Aoede, Kore, Leda, Charon, Fenrir, Puck, Orus"
                            }
                        },
                        "required": ["voice_name"]
                    }
                }, {
                    "name": "end_call",
                    "description": (
                        "Hang up and end the call. Call this ONLY when the conversation "
                        "is clearly over -- the caller said goodbye, said that is all, or "
                        "there is nothing left to do. Always say a brief, warm goodbye "
                        "FIRST, then call end_call to disconnect."
                    ),
                    "parameters": {"type": "object", "properties": {}}
                }, {
                    "name": "new_conversation",
                    "description": (
                        "Start a fresh conversation. Call this when the caller asks to "
                        "start over, start a new dialogue, forget the current thread, or "
                        "clear the conversation. It wipes THIS running conversation only "
                        "-- what you knew about them (their name, their history with you) "
                        "is kept. After calling it, greet them anew and continue from a "
                        "clean slate. Do NOT call it just because the caller changes "
                        "topic."
                    ),
                    "parameters": {"type": "object", "properties": {}}
                }]
            }]

        # Everything ELSE this agent is granted, straight from the node catalogue
        # -- the same source brin builds its chat tools from (see brin's
        # catalog_tools.py: "no hardcoded tool list -- change a grant in the DB
        # and the agent's tools change with no code edit"). Voice used to be the
        # one channel that ignored it, so the SAME agent had different powers
        # depending on whether you wrote to it or called it: John held planner
        # rw- and a contract explaining it, and by phone still could not file a
        # task. He invented a recipient instead and nothing refused it.
        # The five declarations above stay compiled in: two are transport
        # (set_voice, end_call) and messaging is what the catalogue cannot
        # express, since its paths carry an id.
        _cat_decls, _cat_dispatch = await fetch_catalog_tools(brin_agent)
        if _cat_decls:
            if brin_tool_decl:
                brin_tool_decl[0]["function_declarations"].extend(_cat_decls)
            else:
                # No brin, but granted api tools still work: they are called on
                # core-api, not through brin.
                brin_tool_decl = [{"function_declarations": _cat_decls}]
            print("[catalog] %s: +%d tool(s): %s"
                  % (brin_agent, len(_cat_decls), ", ".join(d["name"] for d in _cat_decls)))

        setup_inner = {
            "model": f"models/{model}",
            "generation_config": {
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {"voice_name": voice}
                    },
                    "language_code": _call_lang[0]
                }
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
        }
        if brin_tool_decl:
            setup_inner["tools"] = brin_tool_decl
            # AUTO (default): the model decides whether to put work in hand
            # (send_message), look up a person's history (messages_search), change
            # voice, or just talk. NOTE: a false claim is NOT structurally impossible
            # here, whatever
            # an earlier comment said -- the failure seen in practice is the model
            # saying "I am sending it" and calling nothing at all. The prompt carries
            # that rule; VOICE_TOOL_MODE=ANY/NONE forces a call every turn if needed.
            _tmode = os.environ.get("VOICE_TOOL_MODE", "AUTO").strip().upper()
            if _tmode in ("ANY", "NONE"):
                setup_inner["tool_config"] = {
                    "function_calling_config": {
                        "mode": _tmode,
                        "allowed_function_names": ["send_message", "messages_search"],
                    }
                }
        setup = {"setup": setup_inner}
        await gemini_ws.send(json.dumps(setup))
        setup_resp = await gemini_ws.recv()
        print(f"[WS] Step 7 OK: Gemini setup complete")

        # Session transcript from browser SpeechRecognition
        session_transcript = []

        # Step 8: Ready
        await log_message(session_id, "server", "gemini_connected")
        await client_ws.send_json({"type": "status", "text": "ready", "voice": voice})
        print(f"[WS] Step 8: sent 'ready' to client. Session {session_id} fully initialized.")

        # Trigger Gemini to speak first. Greet only on a real arrival; a quick reconnect
        # after a drop should resume, not re-greet. Decide by the gap since this uid's
        # previous session (< 10 min => reconnect; else new/return => greet).
        _gap_min = None
        try:
            async with aiosqlite.connect(DB_PATH) as _gdb:
                _gr = await _gdb.execute_fetchall(
                    "SELECT started_at FROM sessions WHERE uid=? AND id<>? ORDER BY started_at DESC LIMIT 1",
                    (uid, session_id))
            if _gr:
                _prev = datetime.fromisoformat(str(_gr[0][0]).replace("Z", "+00:00"))
                if _prev.tzinfo is None:
                    _prev = _prev.replace(tzinfo=timezone.utc)
                _gap_min = (datetime.now(timezone.utc) - _prev).total_seconds() / 60.0
        except Exception as _ge:
            print("[greet] gap calc failed: %s" % _ge)
        if _gap_min is not None and _gap_min < 10:
            _greet_txt = ('(the caller just RECONNECTED after a brief drop, only minutes ago. Do NOT greet '
                          'again and do NOT re-introduce yourself. Say ONE short line that you are back, in '
                          'the SAME language you greeted in at the start of this call (%s; NEVER switch to another language on your own just because of the caller name), for example "I am back, let us continue", then wait for them. Do ' % _call_lang[1] +
                          'not summarize the past conversation.)')
        else:
            _greet_txt = ('(the caller just connected. Greet them briefly and warmly, by name if you know it. '
                          'Do NOT summarize past conversations and do NOT claim any past action. Then wait for '
                          'them to speak.)')
        greet_msg = {
            "client_content": {
                "turns": [{"role": "user", "parts": [{"text": _greet_txt}]}],
                "turn_complete": True
            }
        }
        await gemini_ws.send(json.dumps(greet_msg))
        print("[WS] Step 9: greeting gap_min=" + (str(round(_gap_min, 1)) if _gap_min is not None else "new"))

        # --- instrumentation + single-writer lock on the Gemini WS ---
        # Two coroutines write to gemini_ws (audio loop + the tool_response task);
        # websockets forbids concurrent sends -> interleaved frames -> 1011. Serialize
        # every send through one lock and log a timestamped event trail (EV).
        import time as _time
        _ev_n = [0]
        _ev_t0 = _time.monotonic()
        _last = {"recv": "-", "send": "-"}
        _audio_sends = [0]
        _cancelled = set()  # tool-call ids Gemini cancelled (user interrupted)
        def _ev(tag):
            _ev_n[0] += 1
            print(f"[EV {_ev_n[0]:04d} t={_time.monotonic()-_ev_t0:6.2f}] {tag}", flush=True)
        _spoke = [False]
        _bot_buf = []  # accumulate the assistant's streamed transcript fragments
        _said = [""]   # the line actually SPOKEN this turn, kept until the turn ends.
                       # Gemini flushes it on generationComplete and closes the turn in a
                       # LATER message (turnComplete), so the false-claim guard -- which
                       # runs at the turn boundary -- always found an empty buffer and
                       # never fired once in the node's whole life.
        _user_buf = []  # accumulate the user's streamed input-transcription fragments
        _last_user_ts = [time.monotonic()]  # when the USER last spoke (idle watchdog)
        _checked_in = [False]  # idle watchdog already asked "anything else?"
        _my_sent_ids = set()  # bus ids of messages THIS agent originated (so the relay
                              # watcher does not announce the agent's own delegations)

        def _is_answer_for_me(m, any_channel: bool = False) -> bool:
            """Is this bus row an answer this call should read out?

            Inbound internal mail addressed to me. A colleague's answer is the easy
            case. The hard one is my OWN back office: the front line and the CLI are
            the same agent, so my request and its answer have identical sender and
            recipient. They are told apart by the REPLY LINK -- an answer carries
            metadata.reply_to_message_id (core stamps it when the agent replies), a
            fresh request does not. Not by the origin stamp: core deliberately copies
            origin onto the reply so the chain knows who is still waiting, which made
            an earlier attempt at this discard every answer it was meant to catch.

            Status is deliberately NOT part of the test. The back office's own wake
            reads the thread and marks the answer `processed` within a minute, and
            read-state is per agent, not per channel -- so filtering on new/routed
            hid every answer that arrived while the caller was still on the line.
            De-duplication is by id (the caller must not hear the same answer twice),
            which is what the status filter was really standing in for.
            """
            if m.get("direction") != "inbound":
                return False
            # The live relay only speaks internal mail (an answer to something this
            # call put in hand). "Anything new for me?" means every channel: a
            # customer's email is news the caller wants too.
            if not any_channel and m.get("channel_type") != "internal":
                return False
            if m.get("recipient") != brin_agent:
                return False
            sender = (m.get("sender") or "")
            if not sender:
                return False
            if sender == brin_agent:
                _meta = m.get("metadata") or {}
                if not (_meta.get("reply_to_message_id") or _meta.get("in_reply_to")):
                    return False          # my own request, not an answer to it
                if m.get("id") in _my_sent_ids:
                    return False
            return True

        _tool_called_turn = [False]  # did a real toolCall happen this model turn?
        _action_results = []  # Async delivery: finished background ask_brin results awaiting announce
        async def _cev(text):
            """Push a human-readable debug event to the page (listening/tool/speaking)."""
            try:
                await client_ws.send_json({"type": "event", "text": text})
            except Exception:
                pass
        async def _tr(role, text):
            """Stream a finalized transcript turn to the client feed. Never leak code/tags:
            strip any <...> token and our [...] markers; if the turn was pure control,
            drop it silently; if something code-like still survives, show a neutral
            system placeholder instead of raw text."""
            if not text:
                return
            import re as _re2
            t = _re2.sub(r"<[^>]*>", "", text)                                  # any <...> tag/token
            t = _re2.sub(r"\[(?:CAPABILITY|FORWARD)[^\]]*\]?", "", t, flags=_re2.I)  # our bracket control markers
            t = t.strip()
            if not t:
                return                                                          # pure control -> drop silently
            if _re2.search(r"[<>{}]|\bctrl\s*\d", t):                           # a leak we could not clean -> mask it
                try:
                    await client_ws.send_json({"type": "chip", "kind": "system", "text": "system message"})
                except Exception:
                    pass
                return
            try:
                await client_ws.send_json({"type": "transcript", "role": role, "text": t})
            except Exception:
                pass
        async def _chip(kind, text):
            """Surface an agent ACTION as a chip in the transcript feed (sent/received/...)."""
            try:
                await client_ws.send_json({"type": "chip", "kind": kind, "text": text})
            except Exception:
                pass
        async def _flush_user():
            """Join the user's buffered input-transcription fragments into ONE phrase
            and record it once. 2.5 native-audio streams the transcript syllable by
            syllable; without this each syllable became its own log line and bus row.
            No-op when the buffer is empty (safe to call at any boundary)."""
            if not _user_buf:
                return
            phrase = " ".join("".join(_user_buf).split())  # raw-concat: 2.5 deltas carry own spacing; space-join split words ("hel lo")
            _user_buf.clear()
            if not phrase:
                return
            _last_user_ts[0] = time.monotonic()  # the caller is active -> reset idle watchdog
            _checked_in[0] = False
            session_transcript.append("USER: " + phrase)
            # record this turn to the registry NOW, not only at hangup.
            asyncio.create_task(record_voice_turn_live(
                _caller, session_id, brin_agent, len(session_transcript) - 1, "user", phrase))
            try:
                await log_message(session_id, "in", "transcript", text=phrase)
            except Exception:
                pass
            print(f"[Session {session_id}] User said: {phrase[:120]}")
            await _cev("heard: " + phrase)
            await _tr("user", phrase)
        gemini_send_lock = asyncio.Lock()
        async def _gsend(obj, tag):
            _last["send"] = tag
            if tag == "audio":
                _audio_sends[0] += 1
                if _audio_sends[0] % 100 == 1:
                    _ev(f"SEND audio (#{_audio_sends[0]})")
            else:
                _ev(f"SEND {tag}")
            async with gemini_send_lock:
                await gemini_ws.send(json.dumps(obj))

        async def client_to_gemini():
            try:
                while True:
                    data = await client_ws.receive_json()
                    if data.get("type") == "transcript":
                        # Browser SpeechRecognition transcript
                        text = data.get("text", "").strip()
                        if text:
                            session_transcript.append(text)
                            await log_message(session_id, "in", "transcript", text=text)
                            print(f"[Session {session_id}] Transcript: {text[:80]}")
                        continue
                    elif data.get("type") == "audio_chunk":
                        raw_size = len(data["audio"]) * 3 // 4
                        cost["input_bytes"] += raw_size
                        msg = {
                            "realtime_input": {
                                "audio": {
                                    "data": data["audio"],
                                    "mime_type": "audio/pcm;rate=16000"
                                }
                            }
                        }
                        await _gsend(msg, "audio")
                    elif data.get("type") == "text":
                        await log_message(session_id, "in", "text", text=data["text"])
                        msg = {
                            "client_content": {
                                "turns": [{"role": "user", "parts": [{"text": data["text"]}]}],
                                "turn_complete": True
                            }
                        }
                        await _gsend(msg, "text")
                    elif data.get("type") == "update_name":
                        uname = data.get("name", "").strip()[:100]
                        if uname:
                            await update_user(uid, name=uname)
                            await log_message(session_id, "server", "name_set", text=uname)
            except (WebSocketDisconnect, Exception) as e:
                print(f"[Session {session_id}] Client done: {e}")
                # Close Gemini WS so gemini_to_client loop exits too
                if gemini_ws:
                    print(f"[Session {session_id}] Closing Gemini WS from client handler...")
                    await gemini_ws.close()

        async def _ask_brin(request_text: str):
            """Hand a request to Brin (this line's node agent). Returns (ok, payload):
            (True, content) on success, (False, error_text) on failure/timeout. Runs
            in the background, so the 150s bound never blocks the voice
            turn -- failures are intercepted by _ask_brin_bg into an IT ticket."""
            # Time the brin round-trip: ask_brin is the one slow tool (it spins a full
            # agent), so the debug strip shows how long it ran and what it returned --
            # request -> seconds -> answer, on the same flow.
            _t0 = time.monotonic()
            await _cev("brin <- " + (request_text or "")[:60])
            try:
                _principal = client_ws.headers.get("remote-user", "").strip()
                if _principal:
                    await _cev("auth: " + _principal)
                import httpx as _httpx
                async with _httpx.AsyncClient(timeout=150.0) as c:
                    r = await c.post(
                        f"{brin_url}/api/message",
                        headers={"X-API-Key": brin_api_key, "Content-Type": "application/json"},
                        json={"message": request_text, "agent": brin_agent, "channel": "voice",
                              "context": {"user_id": f"voice:{uid}", **({"principal": _principal} if _principal else {})}, "stream": False},
                    )
                    if r.status_code != 200:
                        print(f"[brin] error {r.status_code}: {r.text[:200]}")
                        await _cev("brin FAILED in %.1fs: HTTP %s" % (time.monotonic() - _t0, r.status_code))
                        return (False, f"brin HTTP {r.status_code}: {r.text[:500]}")
                    data = r.json()
                    for _a in (data.get("actions") or []):
                        try:
                            _nm = _a.get("name", "?")
                            _in = json.dumps(_a.get("input") or {}, ensure_ascii=False)
                            await _cev("brin: " + _nm + " " + _in[:80])
                        except Exception:
                            pass
                    _content = data.get("content") or "[Brin returned no content]"
                    await _cev("brin done in %.1fs: %s" % (time.monotonic() - _t0, _content[:80]))
                    return (True, _content)
            except _httpx.TimeoutException:
                await _cev("brin TIMEOUT in %.1fs" % (time.monotonic() - _t0))
                return (False, "timeout: brin did not respond within 150s")
            except Exception as e:
                print(f"[brin] call failed: {e}")
                await _cev("brin FAILED in %.1fs: %s" % (time.monotonic() - _t0, type(e).__name__))
                return (False, f"{type(e).__name__}: {e}")

        async def _create_ticket(request_text: str, error_text: str):
            """Error interception: deterministically file an IT ticket for a failed
            voice action (project it=4, tag internal:voice), independent of brin. The
            backend does this automatically -- the model never decides to report."""
            P = "http://smart-node-planner:8080"
            H = {"Remote-User": "voice", "Content-Type": "application/json"}
            # From planner 0.4.0 the port requires a credential. Bound to
            # the `voice` actor and non-delegating, so a ticket is authored
            # `voice` and voice cannot author as anybody else. Written into the
            # companion .env by voice.sh from the key tracking minted.
            _pk = os.environ.get("PLANNER_API_KEY", "").strip()
            if _pk:
                H["X-API-Key"] = _pk
            try:
                import httpx as _httpx
                title = "Voice action failed: " + (request_text[:80] or "(empty)")
                desc = (
                    "Auto-captured failure of a voice action (error interception).\n\n"
                    f"Request: {request_text}\n\nError: {error_text}\n\n"
                    f"Session: {session_id}\nUser uid: {uid}\n\n"
                    "Recent transcript:\n" + "\n".join(session_transcript[-25:])
                )
                async with _httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.post(f"{P}/api/v1/tasks", headers=H, json={
                        "title": title[:200], "description": desc[:6000],
                        "project_id": 4, "priority": "high", "requester": "voice"})
                    if r.status_code not in (200, 201):
                        print(f"[ticket] create failed {r.status_code}: {r.text[:200]}")
                        return None
                    tid = (r.json() or {}).get("id")
                    if tid:
                        try:
                            await c.post(f"{P}/api/v1/tasks/{tid}/tags", headers=H,
                                         json={"tag": "internal:voice"})
                        except Exception as _tg:
                            print(f"[ticket] tag failed: {_tg}")
                        _ev(f"ticket #{tid} created")
                    return tid
            except Exception as _terr:
                print(f"[ticket] error: {_terr}")
                return None

        _ASK_BRIN_ACK = (
            "ACCEPTED: the action is running in the background. Tell the user BRIEFLY "
            "that you are on it (e.g. 'one moment, doing it'), and keep talking. "
            "Do NOT say it is done/sent/created/booked -- the result will arrive as a "
            "separate '[ACTION DONE]' message, and only then do you confirm."
        )

        async def _maybe_flush_actions():
            """Announce finished background actions, but only at a pause (not mid-speech),
            so we never talk over the assistant. Called on completion and on every turnComplete.
            Each queued item is a fully-formed instruction for Gemini to relay."""
            if not _action_results or _spoke[0]:
                return
            msg = "\n\n".join(_action_results)
            _action_results.clear()
            # B2: these updates arrive asynchronously, outside the caller's turn, so
            # Gemini treats the injected turn as a fresh opening and re-greets the caller
            # by name. Anchor it as mid-call so it just continues.
            msg = ("[You are already mid-call with this caller -- do NOT greet, do NOT say "
                   "hi, do NOT open with their name. Just continue naturally and give the "
                   "update.]" + chr(10) + chr(10) + msg)
            try:
                await _gsend({"client_content": {"turns": [{"role": "user", "parts": [{"text": msg[:8000]}]}], "turn_complete": True}}, "action_done")
                _ev("action_done injected")
            except Exception as _aerr:
                print(f"[action] inject failed: {_aerr}")

        async def _verify_action(since_iso: str, actor: str):
            """Inline-verify: ground the announce on the REAL tool event,
            not brin's prose (which can inherit claude's hallucinated success). Query
            the node event log for tool terminals from the active brin agent
            (agent:<slug>, per persona) in this action's
            window. Returns (verdict, detail): 'fail' if a tool.failed appeared (a
            real failure -- this is how a false 'done' gets caught); 'ok' if a
            tool.succeeded did; 'none' if no tool terminal (a plain answer / chat, or
            an uninstrumented tool -- nothing to contradict brin)."""
            try:
                async with httpx.AsyncClient(timeout=4.0) as c:
                    r = await c.get(f"{CORE_API_URL}/api/events",
                                    params={"actor": actor, "since": since_iso, "limit": 40},
                                    headers=_owner_read_headers())
                evs = (r.json() or {}).get("events", []) if r.status_code == 200 else []
            except Exception as _ve:
                print(f"[verify] query failed: {_ve}")
                return ("none", None)
            failed = [e for e in evs if e.get("event_type") == "tool.failed"]
            succ = [e for e in evs if e.get("event_type") == "tool.succeeded"]
            if failed:
                p = failed[0].get("payload") or {}
                return ("fail", f"{failed[0].get('entity_id')}: {p.get('error', '')}"[:180])
            if succ:
                p = succ[0].get("payload") or {}
                return ("ok", f"{succ[0].get('entity_id')}: {str(p.get('result', ''))[:120]}")
            return ("none", None)

        async def _ask_brin_bg(request_text):
            """Run a brin action OFF the conversation turn, then queue an announcement
            for the next pause. Decouples 15-150s brin latency from the voice turn: the
            user keeps talking, the action lives independently, and speaking no longer
            cancels it. The announce is grounded on the real tool event (inline-verify),
            not brin's word; a real failure -> honest report + IT ticket."""
            from datetime import datetime, timezone, timedelta
            since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
            ok, payload = await _ask_brin(request_text)
            verdict, detail = await _verify_action(since, f"agent:{brin_agent}")
            if verdict == "fail" or not ok:
                # A real tool.failed event (catches a false 'done'), or brin itself
                # failed -> honest report + ticket. Events override brin's prose.
                tid = await _create_ticket(request_text, detail or payload)
                ref = f", ticket #{tid}" if tid else ""
                item = ("[ACTION FAILED] Tell the user honestly and calmly: there was a "
                        "problem with this, you have already handed it to our tech team" + ref + ". "
                        "Do NOT invent success, do not say it is done.")
            else:
                # verdict 'ok' (a tool.succeeded confirms it) or 'none' (a plain answer
                # / non-action -- nothing contradicts brin). Relay brin's result.
                item = ("[ACTION DONE] " + payload + "\n"
                        "Tell this to the user now, briefly and naturally.")
            _action_results.append(item)
            await _maybe_flush_actions()

        async def _run_tool_calls(function_calls):
            """Execute Gemini function calls OFF the receive loop, then send the
            tool_response. Runs as a background task so the Live WS keeps
            being read while brin works; blocking the loop triggers a 1011 session
            crash. ask_brin is acked immediately and run in the background (see
            _ask_brin_bg); set_voice and the rest answer inline."""
            function_responses = []
            for fc in function_calls:
                fname = fc.get("name", "")
                fargs = fc.get("args", {}) or {}
                fc_id = fc.get("id", "")
                print(f"[Session {session_id}] Tool call: {fname}({json.dumps(fargs, ensure_ascii=False)[:200]})")
                await _cev("tool: " + fname)
                await emit_event("agent:%s" % (brin_agent or "voice"), "voice.tool.invoked",
                                 "tool", fname,
                                 {"session_id": session_id,
                                  "args": json.dumps(fargs, ensure_ascii=False)[:400]})
                if fname == "send_message":
                    # Thin delegate: drop a message on internal mail and return at once.
                    # 'self' -> this persona's own back-office agent (same slug); the bus
                    # wakes the CLI agent, which does the real work and replies to mail.
                    _to = (fargs.get("to") or "").strip()
                    _msg = (fargs.get("message") or "").strip()
                    if not _to or not _msg:
                        result = "[ERROR] send_message needs both 'to' and 'message'"
                    else:
                        _dest = brin_agent if _to.lower() in ("self", "myself", "me") else _to.lower()
                        try:
                            import httpx as _httpx
                            async with _httpx.AsyncClient(timeout=10.0) as c:
                                _r = await c.post(
                                    f"{CORE_API_URL}/api/messages/send",
                                    # PROVE THE SERVICE, THEN NAME THE AGENT.
                                    # `Remote-User: <agent>` alone stopped working on
                                    # 2026-08-14: core-api made an agent's name unforgeable
                                    # by refusing it over TCP, correctly -- a name any
                                    # process on the trusted network can type is not a
                                    # capability. Voice is a container and can never be
                                    # the agent's own process, so it presents its own
                                    # credential and says who it acts for; core-api checks
                                    # both and records the service on the row. Remote-User
                                    # is kept so a node without the service secret
                                    # configured fails the way it did rather than in some
                                    # new way.
                                    headers=_svc_headers(brin_agent),
                                    # Stamp where this came from. Two jobs: the recipient
                                    # sees a caller is waiting on a line (not a colleague
                                    # writing at leisure), and -- when the recipient is my
                                    # own back office -- it is what tells my delegation
                                    # apart from its answer, since both carry my slug on
                                    # both sides. `conversation` is the origin contract
                                    # consumed by core-api. Calling it `caller` made the
                                    # pending relay indistinguishable from an answer with
                                    # no person waiting for it.
                                    # Ask for the answer explicitly, in code rather than
                                    # leaving it to the model's wording. A question sent on
                                    # internal mail always came back; an ACTION did not --
                                    # the back office did the work and closed the message
                                    # silently, so the line could not tell "done" from "not
                                    # started" and told the caller it had not happened,
                                    # a minute after the email had reached them.
                                    json={"to": _dest,
                                          "body": _msg + "\n\nWhen this is done, reply in this "
                                                         "thread saying what you did. Someone is "
                                                         "on a call waiting for the answer.",
                                          "channel": "internal",
                                          # The call's real address is the caller's
                                          # voice address, which core-api resolves from
                                          # `caller`; `voice:<uid>` stays only as the
                                          # fallback when it cannot (#9). `call` is the
                                          # session, so an answer that lands after the
                                          # hang-up is mailed to the owner instead.
                                          "origin": {"channel": "voice", "conversation": "voice:%s" % uid,
                                                     "caller": _caller, "call": session_id}},
                                )
                            if _r.status_code == 200:
                                await _cev("sent -> " + _dest)
                                await _chip("send", "Sent to " + _dest)
                                try:
                                    _sid = (_r.json() or {}).get("id")
                                    if _sid is not None:
                                        _my_sent_ids.add(_sid)  # don't relay my own delegation back
                                except Exception:
                                    pass
                                result = ("Queued on internal mail to %s. Acknowledge in ONE short, natural, COMPLETE sentence "
                                          "that you are on it (e.g. 'one moment, I am checking on that for you'). Do NOT read "
                                          "the message back, and do NOT restate the topic or the colleague's name as a "
                                          "trailing fragment. The reply comes back to your mail -- do "
                                          "NOT say it is done yet." % _dest)
                            else:
                                # Include the body core-api returned: it names WHY
                                # the send was refused (e.g. "no active agent 'pm (project-manager)'
                                # on this node"), not just the status code. Keep it short
                                # for a voice turn; 200 chars fits a spoken acknowledgment.
                                _err_body = (_r.text or "")[:200]
                                await _cev("send FAILED HTTP %s: %s" % (_r.status_code, _err_body))
                                result = "[ERROR] send failed: HTTP %s: %s" % (_r.status_code, _err_body)
                        except Exception as _se:
                            await _cev("send FAILED " + type(_se).__name__)
                            result = "[ERROR] send failed: %s" % type(_se).__name__
                elif fname == "set_voice":
                    _vn = (fargs.get("voice_name") or "").strip().lower()
                    _vinfo = VOICES.get(_vn)
                    if _vinfo:
                        try:
                            _np = dict(_p) if isinstance(_p, dict) else {}
                            _np["voice"] = _vinfo["id"]
                            await update_user(uid, preferences=_np)
                            await log_message(session_id, "server", "voice_change", text=_vinfo["id"])
                            await client_ws.send_json({"type": "voice_change", "voice": _vinfo["id"]})
                            result = f"Voice switching to {_vinfo['id']} now -- tell the caller briefly you are switching."
                        except Exception as _ve:
                            result = f"[ERROR] could not switch voice: {_ve}"
                    else:
                        result = "Unknown voice. Valid: Aoede, Kore, Leda, Charon, Fenrir, Puck, Orus."
                elif fname == "end_call":
                    # The agent decided the conversation is over. Let the goodbye it
                    # already said play, then hang up by closing the client socket.
                    await _cev("ending call")
                    async def _end_call_soon():
                        await asyncio.sleep(5)  # let the goodbye finish
                        try:
                            # Tell the client this was an intentional end so it stops
                            # cleanly and does NOT auto-reconnect.
                            await client_ws.send_json({"type": "end_call"})
                        except Exception:
                            pass
                        try:
                            await client_ws.close()
                        except Exception:
                            pass
                    asyncio.create_task(_end_call_soon())
                    result = "Ending the call now. Say a brief warm goodbye if you have not already."
                elif fname == "new_conversation":
                    # Wipe THIS running thread (memory + disk) so the agent stops
                    # resuming it. Durable memory (profile, cross-channel history) is
                    # untouched. This is the escape hatch the caller asked for and the
                    # way out of a thread the agent has talked itself into a corner on.
                    window_clear(wkey)
                    conversation_history.pop(session_id, None)
                    await _cev("new conversation -- window cleared")
                    result = ("Conversation cleared. Start fresh: greet the caller warmly "
                              "as if this were a new call, and do not refer back to "
                              "anything said before this point.")
                elif fname in _cat_dispatch:
                    # A granted api tool from the node catalogue. Called AS the
                    # agent -- core-api re-checks the grant on every request and
                    # ledgers it, so this path cannot exceed what the agent is
                    # allowed no matter what the model asks for.
                    _method, _url = _cat_dispatch[fname]
                    # A person-scoped lookup is about the person ON THE LINE, and
                    # WHO that is is the authenticated caller -- not a name the model
                    # guessed (it passed the owner's full name, which resolves to no
                    # handle, or narrowed to the wrong day). Fill identity from the
                    # verified caller so the tool always looks up the right person.
                    if fname == "messages_search" and _caller:
                        fargs = dict(fargs or {})
                        fargs["identity"] = _caller
                    try:
                        import httpx as _httpx
                        async with _httpx.AsyncClient(timeout=20.0) as c:
                            # A read tool's arguments belong in the query string; a
                            # body on a GET is ignored by every server, so the tool
                            # would silently return an unfiltered list instead of
                            # what was asked for.
                            if _method == "GET":
                                _r = await c.request(_method, _url,
                                                     params={k: v for k, v in (fargs or {}).items()
                                                             if v not in (None, "")},
                                                     headers=_svc_headers(brin_agent))
                            else:
                                _r = await c.request(_method, _url, json=fargs or {},
                                                     headers=_svc_headers(brin_agent))
                        _body = (_r.text or "")[:1500]
                        if 200 <= _r.status_code < 300:
                            await _cev("%s ok" % fname)
                            await _chip("tool", fname.replace("_", " "))
                            # State plainly that it is DONE. The neighbouring
                            # send_message says the opposite ("do NOT say it is
                            # done") because it only queues; this one completed,
                            # and blurring the two is how a caller ends up told
                            # a task exists when nothing was filed.
                            if _method == "GET":
                                # A read: answer FROM the result, do not report that a
                                # read happened. "I looked it up" is not an answer.
                                result = ("[HTTP %s] %s\nThis is the real answer. Tell the caller what it "
                                          "says, briefly and in their language. If it is empty, say there "
                                          "is nothing." % (_r.status_code, _body))
                            else:
                                result = ("[HTTP %s] %s\nThis is DONE, not queued. Tell the caller briefly, "
                                          "in ONE sentence, what now exists." % (_r.status_code, _body))
                        else:
                            print("[catalog] %s -> HTTP %s: %s" % (fname, _r.status_code, _body[:200]))
                            result = ("[ERROR] %s failed with HTTP %s: %s. Tell the caller plainly that it "
                                      "did NOT work. Never say it is done." % (fname, _r.status_code, _body[:300]))
                    except Exception as _te:
                        print("[catalog] %s dispatch failed: %s" % (fname, _te))
                        result = ("[ERROR] %s could not be reached: %s. Tell the caller it did NOT work."
                                  % (fname, _te))
                else:
                    result = f"[ERROR] unknown tool '{fname}'"
                function_responses.append({
                    "id": fc_id, "name": fname, "response": {"result": result[:8000]},
                })
                await log_message(session_id, "server", "tool_response", text=f"{fname}: {result[:500]}")
                # The outcome is read off the result the model is about to be
                # handed, so the log cannot disagree with what the caller was
                # told. Every failure branch above starts the string with [ERROR].
                _failed = result.lstrip().startswith("[ERROR]")
                await emit_event("agent:%s" % (brin_agent or "voice"),
                                 "voice.tool.failed" if _failed else "voice.tool.succeeded",
                                 "tool", fname,
                                 {"session_id": session_id, "result": result[:300]})
            if function_responses:
                # Gemini may cancel the tool call (the caller talked over it) before brin
                # finishes. Sending a tool_response for a cancelled id crashes the session
                # with 1011 -- drop those, and skip entirely if all were cancelled.
                live = [fr for fr in function_responses if fr.get("id") not in _cancelled]
                if not live:
                    _ev("tool_response DROPPED (cancelled)")
                    return
                try:
                    await _gsend({"tool_response": {"function_responses": live}}, "tool_response")
                except Exception as e:
                    print(f"[Session {session_id}] tool_response send failed: {e}")

        async def gemini_to_client():
            import re as _re
            try:
                async for msg in gemini_ws:
                    response = json.loads(msg)
                    _rk = next((k for k in ("toolCall", "setupComplete", "goAway") if k in response), None) or ("serverContent" if "serverContent" in response else ",".join(list(response.keys())[:3]))
                    _last["recv"] = _rk
                    if _rk != "serverContent":
                        _ev(f"RECV {_rk}")
                    elif isinstance(response.get("serverContent"), dict):
                        for _lk in ("interrupted", "turnComplete", "generationComplete"):
                            if response["serverContent"].get(_lk):
                                _ev(f"RECV sc.{_lk}")
                                await _flush_user()  # turn boundary -> commit the user's phrase once
                                if _lk == "interrupted":
                                    # User barged in: tell the client to drop queued + playing audio.
                                    try:
                                        await client_ws.send_json({"type": "interrupt"})
                                    except Exception:
                                        pass
                                _flushed = ""
                                if _bot_buf:
                                    _flushed = " ".join("".join(_bot_buf).split())  # raw-concat: avoid syllable-split on 2.5 (see _flush_user)
                                    if _flushed:
                                        window_add(wkey, BOT_LABEL, _flushed)
                                        session_transcript.append(f"{BOT_LABEL}: {_flushed}")
                                        # record this turn to the registry NOW, not only at hangup.
                                        asyncio.create_task(record_voice_turn_live(
                                            _caller, session_id, brin_agent,
                                            len(session_transcript) - 1, "assistant", _flushed))
                                        # One clean assistant line per turn in voice.db
                                        # (was per-fragment for 2.5). Memory/bus join above.
                                        await log_message(session_id, "out", "transcript", text=_flushed)
                                        await _tr("agent", _flushed)
                                        _said[0] = _flushed   # survives until turnComplete
                                    _bot_buf.clear()
                                if _lk == "turnComplete":
                                    _spoke[0] = False
                                    _line = _flushed or _said[0]
                                    if (os.environ.get("VOICE_GUARD", "on").strip().lower() != "off") and _line and not _tool_called_turn[0] and _claims_action(_line):
                                        _ev("GUARD false-completion -> forcing send_message")
                                        try:
                                            await log_message(session_id, "server", "guard_force", text=_line[:200])
                                            await _gsend({"client_content": {"turns": [{"role": "user", "parts": [{"text": _GUARD_MSG}]}], "turn_complete": True}}, "guard")
                                        except Exception as _gerr:
                                            print(f"[guard] inject failed: {_gerr}")
                                    _tool_called_turn[0] = False
                                    _said[0] = ""
                                    await _cev("listening")
                                    # Per-turn time refresh: the system prompt's clock is
                                    # fixed at session start, so push the CURRENT time as a
                                    # silent context note before the next user turn -- so
                                    # "what time is it" is answered from now, not call-start.
                                    # Env-gated (VOICE_TIME_REFRESH=off) in case mixing a
                                    # context turn with the live audio stream misbehaves.
                                    if os.environ.get("VOICE_TIME_REFRESH", "off").strip().lower() == "on":
                                        try:
                                            from zoneinfo import ZoneInfo as _ZI
                                            _tz = _ZI(client_tz) if client_tz else timezone.utc
                                            _nowt = datetime.now(_tz).strftime("%Y-%m-%d %H:%M %Z")
                                            await _gsend({"client_content": {"turns": [{"role": "user", "parts": [{"text": "[CURRENT TIME (system note, do not read aloud): it is now %s. This is the authoritative current time -- use it, not the time from the start of the call.]" % _nowt}]}], "turn_complete": False}}, "time_update")
                                        except Exception as _terr:
                                            print(f"[time] refresh inject failed: {_terr}")
                                    # Announce any background actions that finished while busy.
                                    await _maybe_flush_actions()
                    if "toolCallCancellation" in response:
                        _ids = response["toolCallCancellation"].get("ids", []) or []
                        _cancelled.update(_ids)
                        _ev(f"CANCELLED {_ids}")
                        continue
                    # Gemini Live tool-call (function calling) -- delegate to Brin and reply.
                    if "toolCall" in response:
                        _tool_called_turn[0] = True
                        await _flush_user()  # user finished -> record their phrase before the tool runs
                        tc = response["toolCall"]
                        function_calls = tc.get("functionCalls", []) or []
                        # Run OFF this loop -- blocking it while brin works drops the
                        # Live session with 1011. The model waits for tool_response,
                        # so there is no double-answer.
                        await _cev(f"{_dname} is calling a tool...")
                        asyncio.create_task(_run_tool_calls(function_calls))
                        continue
                    if "serverContent" in response:
                        sc = response["serverContent"]

                        # Gemini audio transcription (user's speech)
                        input_tr = sc.get("inputTranscription", {})
                        if input_tr and input_tr.get("text"):
                            tr_text = input_tr["text"]  # keep raw spacing; 2.5 native-audio encodes word boundaries inside each delta
                            if tr_text:
                                # Accumulate fragments; _flush_user joins them into one
                                # phrase at the utterance boundary (no per-syllable rows).
                                _user_buf.append(tr_text)
                                window_add_syllable(wkey, tr_text)

                        # Gemini audio transcription (model's speech)
                        output_tr = sc.get("outputTranscription", {})
                        if output_tr and output_tr.get("text"):
                            tr_text = output_tr["text"]  # keep raw spacing (see input); strip only for tag scan + final join
                            if tr_text:
                                # Detect capability tags Gemini may leak into spoken response
                                await parse_capability_tags(uid, session_id, tr_text.strip(), wkey)
                                # Strip tags before storing in user-visible transcript
                                clean = strip_control_tags(tr_text)
                                if clean:
                                    _bot_buf.append(clean)
                                # Do NOT log per chunk: 2.5 native-audio streams the
                                # transcript in fragments (syllable by syllable). We buffer in
                                # _bot_buf and write ONE joined row per turn (flush below).

                        parts = sc.get("modelTurn", {}).get("parts", [])
                        for part in parts:
                            if "inlineData" in part:
                                if not _spoke[0]:
                                    _spoke[0] = True
                                    await _cev(f"{_dname} is speaking...")
                                audio_b64 = part["inlineData"]["data"]
                                raw_size = len(audio_b64) * 3 // 4
                                cost["output_bytes"] += raw_size
                                try:
                                    await client_ws.send_json({
                                        "type": "audio_response",
                                        "audio": audio_b64,
                                    })
                                except Exception:
                                    return  # client gone
                            elif "text" in part:
                                thinking = part["text"]
                                conversation_history.setdefault(session_id, []).append(thinking)
                                await log_message(session_id, "out", "thinking", text=thinking)
                                print(f"[Session {session_id}] Thinking: {thinking[:80]}...")
                                # Flush any pending syllables on thinking (model started responding)
                                window_flush_syllables(wkey, force=True)
                                # Thinking not forwarded to client (security)
                                # Detect access control
                                if not user.get("verified"):
                                    tl_access = thinking.lower()
                                    granted_signals = ["access:granted", "granted access", "access granted",
                                                       "password correct", "correct password", "verified",
                                                       
                                                       "successfully granted", "granting access"]
                                    denied_signals = ["access:denied", "denied access", "access denied",
                                                      "incorrect password", "wrong password",
                                                      
                                                      "denied due to"]
                                    if any(s in tl_access for s in granted_signals):
                                        await update_user(uid, verified=1)
                                        user["verified"] = 1
                                        await log_message(session_id, "server", "access_granted")
                                        print(f"[Session {session_id}] ACCESS GRANTED")
                                    elif any(s in tl_access for s in denied_signals):
                                        await log_message(session_id, "server", "access_denied")
                                        print(f"[Session {session_id}] ACCESS DENIED")

                                # Detect voice change by keywords
                                tl = thinking.lower()
                                if any(w in tl for w in ["switch", "change", "voice", "goloc", "menja", "golos"]):
                                    for vname, vinfo in VOICES.items():
                                        if vname in tl or vinfo["id"].lower() in tl:
                                            new_prefs = dict(_p) if isinstance(_p, dict) else {}
                                            new_prefs["voice"] = vinfo["id"]
                                            await update_user(uid, preferences=new_prefs)
                                            try:
                                                await client_ws.send_json({"type": "voice_change", "voice": vinfo["id"]})
                                            except Exception:
                                                pass
                                            await log_message(session_id, "server", "voice_change", text=vinfo["id"])
                                            print(f"[Session {session_id}] Voice change: {vinfo['id']}")
                                            break

                                # Detect capability gap tags (two-step protocol)
                                # Tags can appear in thinking OR in spoken transcript (Gemini Native Audio leaks them)
                                await parse_capability_tags(uid, session_id, thinking, wkey)
                        if sc.get("turnComplete"):
                            cost["turns"] += 1
                            in_t = cost["input_bytes"] / BYTES_PER_TOKEN_IN
                            out_t = cost["output_bytes"] / BYTES_PER_TOKEN_OUT
                            total = (in_t * INPUT_PRICE + out_t * OUTPUT_PRICE) / 1_000_000
                            try:
                                await client_ws.send_json({
                                    "type": "turn_complete",
                                    "cost": round(total, 6),
                                })
                            except Exception:
                                return  # client gone
            except Exception as e:
                import traceback as _tb
                print(f"[Session {session_id}] Gemini done: {e}")
                _ev(f"GEMINI-CLOSE last_recv={_last['recv']} last_send={_last['send']} err={type(e).__name__}")
                print(_tb.format_exc(), flush=True)

        async def _idle_watch():
            """Keep the call from dying in silence (e.g. while a colleague answer is
            still pending): if the CALLER has been quiet for a while, have the agent ask
            once if there is anything else or whether to wrap up; if still silent after
            that, say a warm goodbye and close. Driven by the caller's activity, not the
            agent's own speech, so the agent's check-in does not reset it."""
            CHECKIN_AFTER = 75.0   # caller silence before the "anything else?" check-in
            CLOSE_AFTER = 75.0     # further silence after the check-in before goodbye
            try:
                while True:
                    await asyncio.sleep(8)
                    if _spoke[0]:
                        continue  # agent is mid-speech -> not idle
                    idle = time.monotonic() - _last_user_ts[0]
                    if not _checked_in[0] and idle >= CHECKIN_AFTER:
                        _checked_in[0] = True
                        _last_user_ts[0] = time.monotonic()  # full window after the check-in
                        try:
                            await _gsend({"client_content": {"turns": [{"role": "user", "parts": [{"text": "[The caller has gone quiet for a bit. Warmly and briefly ask if there is anything else you can help with, or whether you should wrap up the call. Speak %s; do not switch language on your own. Do not read this note aloud.]" % _call_lang[1]}]}], "turn_complete": True}}, "idle_checkin")
                            await _cev("idle: checking in")
                        except Exception as _ie:
                            print(f"[idle] checkin failed: {_ie}")
                    elif _checked_in[0] and idle >= CLOSE_AFTER:
                        try:
                            await _gsend({"client_content": {"turns": [{"role": "user", "parts": [{"text": "[Still no response. Say a brief, warm goodbye -- they can call back anytime. Do not read this note aloud.]"}]}], "turn_complete": True}}, "idle_bye")
                            await _cev("idle: closing")
                        except Exception:
                            pass
                        await asyncio.sleep(5)  # let the goodbye play, then end the call
                        try:
                            await client_ws.send_json({"type": "end_call"})  # clean stop, no reconnect
                        except Exception:
                            pass
                        try:
                            await client_ws.close()
                        except Exception:
                            pass
                        return
            except asyncio.CancelledError:
                return
            except Exception as _we:
                print(f"[idle] watch error: {_we}")

        async def _relay_watch():
            """Proactively relay a colleague's answer the moment it lands, instead of
            waiting for the caller to ask. Polls this persona's internal inbox; a NEW
            inbound that the agent did not originate itself (a real reply) is queued for
            the agent to speak at the next pause, then dismissed so it is not repeated."""
            # Proactive relay runs on EVERY line (the shared line too), not only persona
            # subdomains: the default front desk delegates, so it needs auto-relay by default.
            _seen = set()       # baseline: inbox that already existed at call start
            _primed = [False]   # do not announce the backlog, only what arrives live
            try:
                while True:
                    await asyncio.sleep(8)
                    try:
                        import httpx as _httpx
                        async with _httpx.AsyncClient(timeout=6.0) as c:
                            r = await c.get(f"{CORE_API_URL}/api/messages/inbox",
                                            headers=_svc_headers(brin_agent), params={"limit": 12})
                        if r.status_code != 200:
                            continue
                        d = r.json() or {}
                        # An answer = inbound internal mail addressed to me, from a
                        # colleague OR from my own back office (see _is_answer_for_me:
                        # my delegations carry an origin stamp, answers do not).
                        # Status is not part of the test: the back office's wake marks
                        # the answer processed within a minute, and that used to hide
                        # every answer that landed while the caller was still on the line.
                        replies = [m for m in (d.get("conversation") or []) if _is_answer_for_me(m)]
                        if not _primed[0]:
                            _seen |= {m.get("id") for m in replies}  # ignore pre-call backlog
                            _primed[0] = True
                            continue
                        queued = False
                        _fresh = []   # colleague replies discovered in THIS poll
                        for m in replies:
                            mid = m.get("id")
                            # No read-state write below: a poller must never mark read
                            # (that was the leak that ate an agent's messages).
                            if mid in _my_sent_ids or mid in _seen:
                                continue  # already handled
                            _seen.add(mid)
                            content = (m.get("content") or "").strip()
                            if not content:
                                continue
                            _relayed_ids.add(mid)  # handed to the caller: not mailed at hang-up (#9)
                            _fresh.append((m.get("sender") or "?", content))
                            await _cev("relay <- " + (m.get("sender") or "?"))
                        if _fresh:
                            queued = True
                            await _chip("msg", "New message" + ("" if len(_fresh) == 1 else "s") + " from " + ", ".join(s for s, _ in _fresh))
                            if len(_fresh) == 1:
                                _s, _c = _fresh[0]
                                _action_results.append(
                                    "[%s just replied with the answer you were waiting on: \"%s\". "
                                    "Tell the caller now, briefly, in %s; never switch language on your own.]" % (_s, _c[:400], _call_lang[1]))
                            else:
                                _sum = "; ".join("%s: \"%s\"" % (_s, _c[:160]) for _s, _c in _fresh)
                                _action_results.append(
                                    "[%d colleagues just replied. Give the caller ONE short spoken "
                                    "summary of all of them now, a brief line each, in %s (never switch language on your own): "
                                    "%s]" % (len(_fresh), _call_lang[1], _sum))
                        if queued:
                            await _maybe_flush_actions()
                    except Exception as _re:
                        print(f"[relay] poll error: {_re}")
            except asyncio.CancelledError:
                return

        # The two WS pumps own the session lifetime; _idle_watch/_relay_watch are
        # background `while True` loops. Run the watchers as cancellable tasks and
        # ALWAYS cancel them when the session ends, so a watcher cannot leak past the
        # call and keep polling forever (the leaked relay that ate an agent's messages).
        _watchers = [asyncio.create_task(_idle_watch()), asyncio.create_task(_relay_watch())]
        try:
            await asyncio.gather(client_to_gemini(), gemini_to_client())
        finally:
            for _w in _watchers:
                _w.cancel()
            await asyncio.gather(*_watchers, return_exceptions=True)

    except Exception as e:
        import traceback
        print(f"[WS] Error: {e}")
        print(f"[WS] Traceback:\n{traceback.format_exc()}")
        if session_id:
            await log_message(session_id, "server", "error", text=str(e)[:500])
        try:
            await client_ws.send_json({"type": "error", "text": f"Connection error: {type(e).__name__}. Please try again."})
        except Exception:
            pass
    finally:
        # Close the browser socket FIRST so the client detects the drop and
        # reconnects immediately. Otherwise the heavy end-of-session cleanup
        # below (memory extraction = a 10-30s Gemini call) holds the socket open
        # and the UI only notices the drop ~30-50s later.
        try:
            await client_ws.close()
        except Exception:
            pass
        duration = time.time() - start_time
        # Flush transcript window
        if uid:
            window_flush_syllables(wkey, force=True)
            # Age out BEFORE writing, not at the next call's start. Cleanup used to
            # run on the in-memory copy at Step 4, which after any restart is empty,
            # so it did nothing and the loader then read the whole file back
            # unfiltered -- the age rule had never once applied. Trimming here means
            # the file itself can never hold anything stale, and this runs on a
            # normal hangup and a dropped line alike (it is the finally block).
            window_cleanup(wkey)
            window_dump(wkey)
            # Clear stale pending capability_gap (user disconnected before confirming)
            pending_forwards.pop(uid, None)
        print(f"[WS] Finally block: session_id={session_id}, duration={duration:.1f}s, uid={uid}")
        if session_id:
            in_t = cost["input_bytes"] / BYTES_PER_TOKEN_IN
            out_t = cost["output_bytes"] / BYTES_PER_TOKEN_OUT
            total_cost = (in_t * INPUT_PRICE + out_t * OUTPUT_PRICE) / 1_000_000
            stats = {
                "duration": round(duration, 1),
                "input_bytes": cost["input_bytes"],
                "output_bytes": cost["output_bytes"],
                "input_tokens": round(in_t, 1),
                "output_tokens": round(out_t, 1),
                "cost": round(total_cost, 6),
                "turns": cost["turns"],
            }
            await end_session(session_id, stats)
            await report_spend(brin_agent, session_id, stats)
            await log_message(session_id, "server", "session_end", text=json.dumps(stats))
            await emit_event("component:voice", "voice.call.ended", "call", session_id,
                             {"agent": brin_agent or "", "turns": stats["turns"],
                              "duration_s": stats["duration"], "cost": stats["cost"]})
            # An answer that arrived while the line was up but was never handed to
            # the caller would otherwise sit in the agent's inbox with nobody to say
            # it (#9). core-api mails every answer to this call's hand-offs that is
            # not in the spoken list to the owner, once.
            try:
                async with httpx.AsyncClient(timeout=6.0) as _c:
                    _r = await _c.post(f"{CORE_API_URL}/api/messages/voice-call-ended",
                                       headers=_origin_headers(),
                                       json={"call": session_id, "spoken": sorted(_relayed_ids)})
                    if _r.status_code == 200:
                        _d = _r.json() or {}
                        if _d.get("mailed") or _d.get("not_sent"):
                            print(f"[voice-late] call {session_id}: mailed {_d.get('mailed')}, "
                                  f"not sent {_d.get('not_sent')}")
                    else:
                        print(f"[voice-late] call {session_id}: HTTP {_r.status_code} {_r.text[:200]}")
            except Exception as _e:
                print(f"[voice-late] call {session_id}: {type(_e).__name__}: {_e}")
            print(f"[Session {session_id}] Ended: {stats['duration']}s, ${stats['cost']:.4f}, {stats['turns']} turns")
            try:
                _ru = client_ws.headers.get("remote-user", "").strip()
            except Exception:
                _ru = ""
            if _ru and session_transcript:
                try:
                    await record_voice_to_bus(_ru, list(session_transcript), session_id, brin_agent)
                except Exception as _e:
                    print(f"[voice-bus] record failed: {_e}")

            # Extract memory from thinking texts + save conversation summary
            thinking_texts = conversation_history.get(session_id, [])
            print(f"[Session {session_id}] Thinking texts: {len(thinking_texts)}, Transcripts: {len(session_transcript)}")
            if (thinking_texts or session_transcript) and stats["turns"] > 0:
                # Signal other sessions to wait
                extraction_event = asyncio.Event()
                pending_extractions[uid] = extraction_event
                print(f"[Session {session_id}] Starting memory extraction...")
                try:
                    await extract_memory_from_session(uid, thinking_texts, session_transcript)
                    print(f"[Session {session_id}] Memory extraction completed")
                except Exception as me:
                    print(f"[Session {session_id}] Memory extraction error: {me}")
                finally:
                    extraction_event.set()
                    pending_extractions.pop(uid, None)
                conversation_history[session_id] = []
            else:
                print(f"[Session {session_id}] Skipping extraction: texts={len(thinking_texts)}, turns={stats['turns']}")
        else:
            print(f"[WS] No session_id - connection closed before session was created")
        if gemini_ws:
            await gemini_ws.close()
            print(f"[WS] Gemini WebSocket closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
