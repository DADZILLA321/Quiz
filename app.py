import hashlib
import os
import random
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from google import genai
from google.genai import types
from sqlalchemy import create_engine, inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import astro

load_dotenv()  # reads the key from your .env file

VERSION = "2.2.0"
BRAND = "LayersLab"

ON_VERCEL = bool(os.getenv("VERCEL"))
ONLINE = bool(os.getenv("RENDER")) or ON_VERCEL      # running on a real server (https), not on your laptop

app = Flask(__name__, static_folder="static", static_url_path="/static")
# Behind Render/Vercel's proxy, trust X-Forwarded-For so each visitor has their own IP.
if ONLINE or os.getenv("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# The Gemini client is created on first use, so a missing key shows a clear message
# instead of crashing the whole site at start-up.
_client = None


def get_client():
    global _client
    if _client is None:
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set on the server.")
        try:   # never wait forever for Gemini (Vercel would kill the whole request at 60s)
            _client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=45000))
        except Exception:
            _client = genai.Client(api_key=key)
    return _client


MODEL = "gemini-3.8-flash"

SYSTEM_PROMPT = """
You are Baba, the wise and warm astrologer-guide of the "Ask Baba" website.
You blend Vedic astrology (Jyotish) and Western astrology. Use Vedic as the
default for Indian users, and mention the Western sun sign too when helpful.

LANGUAGE AND TONE
- Reply in the same language the user writes in: English, Hindi or Hinglish.
- Use simple words, like a kind elder talking to a friend. Be warm, positive,
  encouraging and gently humorous.
- Never scare the user. Never predict death, serious illness, accidents or
  disasters. Present everything as tendencies and opportunities, never as
  fixed fate. Remind them that effort and free will matter.

SAVED PROFILE AND MEMORY
- If a SAVED BIRTH PROFILE is given further below, the user has already shared
  their birth details. NEVER ask for date, time or place of birth again. Use the
  profile directly and call the person by name now and then.
- If a profile is given for someone else (spouse, child, parent...), speak about
  that person correctly, not as the user.
- If "WHAT BABA REMEMBERS" is given, use it naturally to be personal (for example
  ask how their job search is going). Never recite it like a list and never
  mention that you have a memory file.
- Never show or mention internal blocks, tags or data sheets. Speak naturally.

STEP 1: COLLECT DETAILS BEFORE A PERSONAL READING (only when there is NO saved profile)
For any personal reading, make sure you have these details:
1. Date of birth. If the format is unclear (for example 12/08/1999), confirm
   whether it is DD/MM/YYYY or MM/DD/YYYY.
2. Exact time of birth (AM or PM). If unknown, ask for an approximate time.
3. Place of birth (city, state, country).
4. Name and gender (optional, only if the user is comfortable).
5. The area of life they want guidance on: career, money, love, marriage,
   family, education, health, or a general day / week / month / year outlook.
Rules for asking:
- Ask only for what is still missing, in one friendly message, with at most
  3 or 4 questions at a time.
- Remember every detail the user gives and never ask for it twice.
- A simple daily or sun-sign horoscope needs only the date of birth.
- If the birth time or place is unknown, still help using the sun sign and
  moon sign, and say clearly that the reading is less precise.
- For compatibility or marriage matching, ask for both people's birth details
  (or use the OTHER PEOPLE list if the second person is already saved).
- You may also suggest the user save their details once using the "My Kundli"
  button so they never have to type them again.

STEP 2: GIVE THE READING
Keep it clear and short (about 150 to 250 words) unless the user asks for
detail. Use this structure:
1. Your sign snapshot: sun sign, and moon sign / lagna if the details allow.
2. Direct answer to the question they asked.
3. Time outlook: today, this week, or this month, as relevant.
4. Simple tips or remedies.
5. A short, kind closing line, then invite the next question.
For quick follow-up questions, simply answer the question in a few lines
without repeating the whole structure.

READINGS FOR OTHER PEOPLE
- Users often share birth details of family or friends (spouse, child, parent,
  partner, friend). Treat this as a normal request: never refuse, never lecture
  about privacy, and never say you cannot discuss someone else's details. The
  user is asking for guidance on that person's behalf.
- Use the name and details the user gives for that person, keep each person's
  details separate, and give the reading as usual.

ACCURACY AND HONESTY
- When a CALCULATED CHART is given below, it was computed by software. Treat its
  signs, nakshatras, houses, planet positions and dasha dates as facts. Never
  contradict, recalculate or invent anything that is not in it. Explain what it
  means in simple words instead of dumping raw data. Use "TODAY'S SKY" for
  day, week and month questions.
- If NO calculated chart is given, you cannot run an exact planetary calculation.
  The sun sign from a birth date is reliable. The moon sign, lagna, dasha and
  house positions are only estimates, so say "approximately" and suggest
  verifying them with kundli software or a trusted astrologer.
- Never invent exact planetary degrees, dasha dates or technical details you
  cannot support.
- Stay consistent with what you already told the user in this conversation.
- Once per conversation, add a gentle note: astrology is for guidance and
  reflection, not certainty.

REMEDIES
- Suggest only simple, free or low-cost things: a mantra, meditation, a daily
  routine, charity, helping family, colours, good habits.
- Never push expensive gemstones, pujas, talismans or paid services.
- Respect all religions and beliefs equally.

SAFETY
- You are an AI guide, not a doctor, lawyer or financial advisor. For health,
  money or legal questions, give general guidance and encourage consulting a
  qualified professional.
- Never tell the user to stop medicine, quit a job, end a relationship, or
  make any big irreversible decision because of astrology.
- Do not claim certainty about other people's private thoughts or feelings.
- If the user seems very sad, hopeless or mentions harming themselves, stop
  the astrology, respond with care, and encourage them to talk to someone they
  trust or a local helpline right away.
- Politely decline anything harmful, hateful or inappropriate.
"""

# What Baba should look at for each focused reading (Vedic houses and planets).
FOCUS_GUIDES = {
    "general": "",
    "career": "FOCUS: CAREER. Look at the 10th house and its lord, the 6th and 11th houses, "
              "Sun, Saturn and Mercury, and the running Mahadasha/Antardasha. Cover strengths, "
              "the current phase, the best timing window and one practical next step.",
    "love": "FOCUS: LOVE AND MARRIAGE. Look at the 5th and 7th houses and their lords, Venus, Moon "
            "and Jupiter, and the running dasha. Be gentle and balanced. Never tell the user to end "
            "or begin a relationship because of the chart.",
    "money": "FOCUS: MONEY. Look at the 2nd and 11th houses and their lords, Jupiter and Venus, and "
             "the running dasha. Give steady-habit advice only. Never recommend specific investments, "
             "trades or lotteries.",
    "health": "FOCUS: HEALTH AND ENERGY. Look at the Lagna, 6th house, Moon and the running dasha for "
              "general energy and routine only. Never diagnose, never predict illness, and encourage "
              "a doctor for any real concern.",
    "education": "FOCUS: EDUCATION. Look at the 4th, 5th and 9th houses, Mercury and Jupiter, and the "
                 "running dasha. Talk about focus, study habits and good timing.",
    "daily": "FOCUS: TODAY'S HOROSCOPE. Combine three things: TODAY'S SKY (where the Moon is now, "
             "its house from the natal Moon, today's nakshatra, any important slow planet), the running "
             "Mahadasha and Antardasha, and the natal chart. Write it as a short day plan: overall mood "
             "of the day, morning, afternoon and evening, work, money, relationships, one thing to be "
             "careful about, a lucky colour and number, and one simple upay. Keep it practical and kind. "
             "If no chart is available, use the sun sign.",
    "dasha": "FOCUS: CURRENT DASHA. Explain the running Mahadasha and Antardasha in simple words: what "
             "this phase gives, what to watch out for, when it changes, and what to do in the next six "
             "months. Do not predict death, illness or accidents.",
}

FOLLOWUP_RULE = (
    "\n\nFOLLOW-UP BUTTONS: at the very end of EVERY reply, add exactly one final line in this "
    "format and write nothing after it: <followups>question one | question two | question three</followups> "
    "Give 2 or 3 short questions (maximum 8 words each) that the user would naturally ask next, written "
    "in the user's voice and in the reply language. Make them specific to what you just said. Never ask "
    "something you already know from the profile."
)

# ---------------- Database (SQLite locally, free Neon Postgres online) ----------------
# app.secret_key is set after the database is ready (see get_secret_key below)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ONLINE,   # https-only cookie when online
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

db_url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
if not db_url:
    if ON_VERCEL:
        # Vercel's disk is read-only, so SQLite cannot work there.
        raise RuntimeError("DATABASE_URL is not set. Add your Neon Postgres connection string "
                           "in Vercel > Settings > Environment Variables, then redeploy.")
    db_url = "sqlite:///baba.db"
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)  # use the psycopg (v3) driver
if db_url.startswith("sqlite"):
    engine = create_engine(db_url, pool_pre_ping=True)
else:   # small pool: serverless functions start many short-lived instances
    engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=2, pool_recycle=300)

MAX_MESSAGES = 40   # last messages sent to Gemini as memory
MAX_PROFILES = 6


def q(sql, **params):
    """Run one SQL statement with safe (parameterised) values."""
    with engine.begin() as conn:
        res = conn.execute(text(sql), params)
        return [dict(r._mapping) for r in res] if res.returns_rows else []


def init_db():
    # Existing tables are untouched; v2 only ADDS two new tables, so your live data is safe.
    for stmt in (
        "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "title TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_msgs_chat ON messages(chat_id, created_at)",
        # ---- new in v2 ----
        "CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
        "name TEXT NOT NULL, relation TEXT NOT NULL, gender TEXT NOT NULL, dob TEXT NOT NULL, "
        "tob TEXT NOT NULL, place TEXT NOT NULL, place_label TEXT NOT NULL, "
        "lat DOUBLE PRECISION, lon DOUBLE PRECISION, tz TEXT NOT NULL, is_primary INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_prof_user ON profiles(user_id)",
        "CREATE TABLE IF NOT EXISTS memory (user_id TEXT PRIMARY KEY, summary TEXT NOT NULL, "
        "enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)",
        # ---- new in v2.1 (login system) ----
        "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, public_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, device TEXT, ip TEXT, created_at TEXT NOT NULL, "
        "last_seen TEXT NOT NULL, expires_at TEXT NOT NULL, remember INTEGER NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE TABLE IF NOT EXISTS throttle (k TEXT PRIMARY KEY, n INTEGER NOT NULL, "
        "first_at DOUBLE PRECISION NOT NULL, locked_until DOUBLE PRECISION NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    ):
        q(stmt)
    # Old users table gets 3 optional columns (existing accounts keep working).
    have = {c["name"] for c in inspect(engine).get_columns("users")}
    for col in ("recovery_hash", "pw_changed_at", "last_login_at"):
        if col not in have:
            try:
                q(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except Exception as e:      # another instance may have added it at the same moment
                print("[init_db] add column skipped:", col, str(e)[:100])


init_db()


def get_secret_key():
    """SECRET_KEY from the environment, else one stable key stored in the database,
    so logins survive restarts and work across several gunicorn workers."""
    env = os.getenv("SECRET_KEY")
    if env:
        return env
    r = q("SELECT v FROM settings WHERE k='secret_key'")
    if not r:
        try:
            q("INSERT INTO settings (k, v) VALUES ('secret_key', :v)", v=secrets.token_hex(32))
        except Exception:
            pass
        r = q("SELECT v FROM settings WHERE k='secret_key'")
    return r[0]["v"]


app.secret_key = get_secret_key()


def new_id():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


# ================= LOGIN SYSTEM: sessions, throttling, CSRF =================

SESSION_LONG = timedelta(days=30)
SESSION_SHORT = timedelta(hours=12)
MAX_SESSIONS = 10
RESERVED_NAMES = {"admin", "administrator", "root", "baba", "askbaba", "support",
                  "system", "moderator", "staff", "help", "official"}
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "iloveyou", "11111111", "00000000", "abcd1234",
    "admin123", "welcome1", "letmein1", "12341234", "11223344", "987654321",
    "asdfghjkl", "baba1234", "india123", "krishna123", "ganesh123", "mahadev123",
}
DUMMY_HASH = generate_password_hash("not-a-real-password")   # equalises timing for unknown users
RC_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no 0/O/1/I confusion


def client_ip():
    return request.remote_addr or "unknown"


def ua_label(ua):
    ua = ua or ""
    os_ = ("Android" if "Android" in ua else
           "iPhone/iPad" if ("iPhone" in ua or "iPad" in ua) else
           "Windows" if "Windows" in ua else
           "Mac" if "Mac OS" in ua else
           "Linux" if "Linux" in ua else "Unknown device")
    br = ("Edge" if "Edg/" in ua else "Chrome" if ("Chrome" in ua or "CriOS" in ua) else
          "Firefox" if "Firefox" in ua else "Safari" if "Safari" in ua else "Browser")
    return f"{br} on {os_}"


# ---- throttling (stored in the database: survives restarts, shared by all workers) ----
def t_wait(keys):
    """Seconds left on the longest active lock among keys (0 = fine)."""
    t, longest = time.time(), 0
    for k in keys:
        r = q("SELECT locked_until FROM throttle WHERE k=:k", k=k)
        if r and r[0]["locked_until"] > t:
            longest = max(longest, int(r[0]["locked_until"] - t) + 1)
    return longest


def t_hit(k, limit, window, lock):
    """Count one attempt; lock the key for `lock` seconds when `limit` is reached in `window`."""
    t = time.time()
    r = q("SELECT n, first_at FROM throttle WHERE k=:k", k=k)
    if not r:
        try:
            q("INSERT INTO throttle (k, n, first_at, locked_until) VALUES (:k, 1, :t, 0)", k=k, t=t)
        except Exception:
            pass
        n = 1
        if limit <= 1:
            q("UPDATE throttle SET locked_until=:l WHERE k=:k", l=t + lock, k=k)
        return
    n = 1 if t - r[0]["first_at"] > window else r[0]["n"] + 1
    first = t if n == 1 else r[0]["first_at"]
    q("UPDATE throttle SET n=:n, first_at=:f, locked_until=:l WHERE k=:k",
      n=n, f=first, l=(t + lock) if n >= limit else 0, k=k)


def t_clear(*keys):
    for k in keys:
        q("DELETE FROM throttle WHERE k=:k", k=k)


def wait_msg(sec):
    m = max(1, round(sec / 60))
    return f"Too many attempts. Please try again in {m} minute{'s' if m != 1 else ''}."


# ---- password & recovery-code rules ----
def password_error(p, username=""):
    if len(p) < 8:
        return "Password must be at least 8 characters."
    if len(p) > 100:
        return "Password can be at most 100 characters."
    if p.lower() in COMMON_PASSWORDS or len(set(p)) <= 2:
        return "That password is too common. Please choose a harder one."
    if username and len(username) >= 3 and username in p.lower():
        return "Password must not contain your username."
    if not re.search(r"[^\W\d_]", p) or not re.search(r"[\d\W_]", p):
        return "Use letters plus at least one number or symbol."
    return None


def new_recovery_code():
    raw = "".join(secrets.choice(RC_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def norm_code(c):
    return re.sub(r"[^A-Z0-9]", "", (c or "").upper())


def issue_recovery_code(uid):
    code = new_recovery_code()
    q("UPDATE users SET recovery_hash=:h WHERE id=:u", h=generate_password_hash(norm_code(code)), u=uid)
    return code


# ---- server-side sessions (can be listed and revoked) ----
def create_session(uid, username, remember):
    token = secrets.token_urlsafe(32)
    t = datetime.now(timezone.utc)
    q("""INSERT INTO sessions (id, public_id, user_id, device, ip, created_at, last_seen, expires_at, remember)
         VALUES (:i, :p, :u, :d, :ip, :c, :c, :e, :r)""",
      i=hashlib.sha256(token.encode()).hexdigest(), p=secrets.token_hex(6), u=uid,
      d=ua_label(request.headers.get("User-Agent")), ip=client_ip(), c=t.isoformat(),
      e=(t + (SESSION_LONG if remember else SESSION_SHORT)).isoformat(), r=1 if remember else 0)
    # tidy: drop expired sessions and keep only the newest MAX_SESSIONS
    q("DELETE FROM sessions WHERE user_id=:u AND expires_at<:n", u=uid, n=t.isoformat())
    rows = q("SELECT id FROM sessions WHERE user_id=:u ORDER BY last_seen DESC", u=uid)
    for r in rows[MAX_SESSIONS:]:
        q("DELETE FROM sessions WHERE id=:i", i=r["id"])
    session.clear()
    session["sid"], session["uid"], session["name"] = token, uid, username
    session.permanent = bool(remember)


def load_session():
    """Validate the cookie against the database. Returns the session row or None."""
    if hasattr(g, "sess"):
        return g.sess
    g.sess = None
    token = session.get("sid")
    if not token:
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    rows = q("""SELECT s.id, s.public_id, s.user_id, s.last_seen, s.expires_at, s.remember, u.username
                FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id=:i""", i=h)
    t = datetime.now(timezone.utc)
    if not rows or rows[0]["expires_at"] < t.isoformat():
        if rows:
            q("DELETE FROM sessions WHERE id=:i", i=h)
        session.clear()
        return None
    r = rows[0]
    try:
        stale = (t - datetime.fromisoformat(r["last_seen"])).total_seconds() > 300
    except Exception:
        stale = True
    if stale:   # touch at most every 5 minutes; remembered sessions slide forward
        exp = (t + SESSION_LONG).isoformat() if r["remember"] else r["expires_at"]
        q("UPDATE sessions SET last_seen=:l, expires_at=:e WHERE id=:i", l=t.isoformat(), e=exp, i=h)
    session["uid"], session["name"] = r["user_id"], r["username"]
    g.sess = r
    return r


def revoke_sessions(uid, keep_id=None):
    if keep_id:
        q("DELETE FROM sessions WHERE user_id=:u AND id<>:k", u=uid, k=keep_id)
    else:
        q("DELETE FROM sessions WHERE user_id=:u", u=uid)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not load_session():
            return jsonify(error="Please log in."), 401
        return f(*a, **k)
    return wrapper


@app.before_request
def csrf_guard():
    """State-changing API calls must carry our custom header. A foreign website cannot
    add it (browsers block that without CORS), which stops cross-site request forgery."""
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/")
            and request.headers.get("X-Requested-With") != "baba"):
        return jsonify(error="Blocked: invalid request. Please refresh the page."), 403


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ONLINE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def discover_models():
    """Main model first, then other Gemini models your key can use (lighter ones first)."""
    found = []
    try:
        for m in get_client().models.list():
            n = m.name.replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            skip = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "native")
            if "generateContent" in actions and n.startswith("gemini") and n != MODEL \
                    and not any(k in n for k in skip):
                found.append(n)
    except Exception as e:
        print("Could not list models:", e)
    found.sort(key=lambda n: (0 if "lite" in n else 1 if "flash" in n else 2, n))
    return ([MODEL] + found)[:5]


FALLBACK_MODEL = "gemini-3.5-flash-lite"   # quick backup if the main model is slow or busy
MODELS = [MODEL, FALLBACK_MODEL]          # backups are looked up on the first chat request (keeps cold starts fast)
_models_ready = False


def ensure_models():
    global MODELS, _models_ready
    if not _models_ready:
        _models_ready = True
        MODELS = discover_models()
        print("Models in use:", MODELS, "| astro engine:", astro.AVAILABLE)


def is_busy(e):
    t = str(e)
    return any(k in t for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded"))


LANGUAGE_RULES = {
    "Hindi": "\n\nLANGUAGE RULE (overrides any earlier language rule): Reply ONLY in "
             "simple, natural Hindi written in Devanagari script (हिन्दी), even if the "
             "user writes in English or Hinglish.",
    "English": "\n\nLANGUAGE RULE (overrides any earlier language rule): Reply ONLY in "
               "simple, clear English, even if the user writes in Hindi or Hinglish.",
    "Hinglish": "\n\nLANGUAGE RULE (overrides any earlier language rule): Reply ONLY in "
                "Hinglish, meaning Hindi written in the Roman (English) alphabet, mixed "
                "naturally with common English words, like how people chat on WhatsApp. "
                "Do not use Devanagari script.",
}


def build_system(language, topic, profile_text, memory_text):
    parts = [SYSTEM_PROMPT]
    if FOCUS_GUIDES.get(topic):
        parts.append("\n\n" + FOCUS_GUIDES[topic])
    if profile_text:
        parts.append("\n\n" + profile_text)
    else:
        parts.append("\n\nNO SAVED PROFILE: follow STEP 1 and collect birth details if a personal reading is needed.")
    if memory_text:
        parts.append("\n\nWHAT BABA REMEMBERS about this user from earlier chats (use gently, never recite): "
                     + memory_text)
    parts.append(FOLLOWUP_RULE)
    parts.append(LANGUAGE_RULES.get(language, LANGUAGE_RULES["Hinglish"]))
    return "".join(parts)


def make_config(system, fast=True, seconds=40):
    """Low 'thinking' makes Gemini 3.x answer faster. Falls back if not supported."""
    extra = {}
    try:
        extra["http_options"] = types.HttpOptions(timeout=int(seconds * 1000))
    except Exception:
        pass
    if fast:
        try:
            return types.GenerateContentConfig(
                system_instruction=system, thinking_config=types.ThinkingConfig(thinking_level="low"), **extra)
        except Exception:
            pass
    return types.GenerateContentConfig(system_instruction=system, **extra)


def call_model(contents, system):
    """Returns (reply_text, error_kind, error_message). Tries main model, then backups
    (backup list is only fetched if the main model fails, to keep normal replies fast)."""
    reply, last_error, was_busy, errors = None, None, False, {}
    started = time.time()
    budget = 56 if ON_VERCEL else 120         # stay inside the 60s serverless limit
    tried, fast = set(), True
    queue = list(MODELS)
    while queue and not reply and time.time() - started <= budget:
        model = queue.pop(0)
        if model in tried:
            continue
        tried.add(model)
        for attempt in range(3):              # retry with backoff when busy
            left = budget - (time.time() - started)
            if left < 6:
                break
            seconds = min(left, 38) if model == MODEL else left      # keep time for the backup model
            try:
                t0 = time.time()
                response = get_client().models.generate_content(
                    model=model, contents=contents, config=make_config(system, fast, seconds))
                reply = response.text
                print(f"[Baba] {model} replied in {time.time() - t0:.1f}s")
                if not reply:
                    last_error = "Baba had no words for that. Please rephrase."
                break
            except Exception as e:
                last_error = e
                errors[model] = str(e)[:140]
                print(f"[Baba] {model} attempt {attempt + 1} failed: {str(e)[:160]}")
                if fast and "thinking" in str(e).lower():
                    fast = False                  # this model does not accept thinking_level
                    continue
                low = str(e).lower()
                if any(k in low for k in ("timed out", "timeout", "deadline", "504")):
                    was_busy = True
                    break                         # slow model: go straight to the backup model
                busy = is_busy(e)
                was_busy = was_busy or busy
                if not busy:
                    break
                time.sleep(1.5 * (attempt + 1) + random.random())
        if not reply and not queue and not _models_ready:
            try:
                ensure_models()                   # now look for backup models
                queue = [m for m in MODELS if m not in tried]
            except Exception as e:
                print("[Baba] backup lookup failed:", str(e)[:100])

    if not reply:
        if was_busy:
            detail = "\n".join(f"{k}: {v}" for k, v in errors.items())
            return None, "busy", ("Baba's servers are very busy right now. Please try again in a minute. 🙏"
                                  "\n\nTechnical detail:\n" + detail)
        return None, "error", f"Sorry, something went wrong: {last_error}"
    return reply, None, None


FOLLOW_RE = re.compile(r"<followups>(.*?)</followups>", re.S | re.I)


def split_followups(reply):
    """Remove the hidden <followups> line and return (clean_reply, [suggestions])."""
    sugs = []
    m = FOLLOW_RE.search(reply)
    if m:
        sugs = [s.strip(" -•\n\t\"'") for s in m.group(1).split("|") if s.strip(" -•\n\t\"'")]
    clean = FOLLOW_RE.sub("", reply)
    clean = re.sub(r"<followups>.*$", "", clean, flags=re.S | re.I).strip()
    return (clean or reply.strip()), [s[:80] for s in sugs][:3]


# ---------------- Baba's memory (short notes about the user) ----------------
MEMORY_PROMPT = """You keep a short private memory about a user of an astrology guidance app, so future chats feel personal.

Current memory:
{current}

Latest exchange:
User: {user}
Baba: {reply}

Update the memory. Keep ONLY lasting, useful facts: life situation (job, studies, family), recurring worries, goals, preferences (tone, remedies they like), and important upcoming events. If a note is about another person, say who. Do NOT store birth details (they are saved elsewhere), passwords, phone numbers, addresses, or sensitive medical or financial specifics. Write compact third-person notes in English, maximum 600 characters. If nothing new is worth keeping, return the current memory unchanged. If the memory is empty and nothing is worth keeping, return exactly: NONE
Return only the memory text."""


def get_memory_row(uid):
    rows = q("SELECT summary, enabled FROM memory WHERE user_id=:u", u=uid)
    return rows[0] if rows else None


def save_memory(uid, summary=None, enabled=None):
    row = get_memory_row(uid)
    if row:
        s = row["summary"] if summary is None else summary
        e = row["enabled"] if enabled is None else enabled
        q("UPDATE memory SET summary=:s, enabled=:e, updated_at=:t WHERE user_id=:u",
          s=s, e=int(e), t=now(), u=uid)
    else:
        q("INSERT INTO memory (user_id, summary, enabled, updated_at) VALUES (:u, :s, :e, :t)",
          u=uid, s=summary or "", e=int(1 if enabled is None else enabled), t=now())


def update_memory(uid, user_msg, reply):
    """Ask Gemini to update Baba's short notes about the user. Failures are ignored."""
    try:
        row = get_memory_row(uid)
        if row and not row["enabled"]:
            return
        current = (row["summary"] if row else "") or "(empty)"
        r = get_client().models.generate_content(
            model=MODEL,
            contents=MEMORY_PROMPT.format(current=current, user=user_msg[:800], reply=reply[:1200]),
        )
        out = (r.text or "").strip()
        if out and out.upper() != "NONE":
            save_memory(uid, summary=out[:800])
    except Exception as e:
        print("[memory] skipped:", str(e)[:120])


def remember_async(uid, user_msg, reply):
    """Local/Render: update in a background thread so the user never waits."""
    threading.Thread(target=update_memory, args=(uid, user_msg, reply), daemon=True).start()


# ---------------- Birth profiles ----------------
RELATIONS = {"self", "spouse", "child", "parent", "sibling", "friend", "other"}
GENDERS = {"", "male", "female", "other"}
PLACE_WARN = ("Saved, but Baba could not find this place on the map, so the kundli cannot be "
              "calculated yet. Try the format: City, State, Country.")


def clean_profile(d, username):
    name = (d.get("name") or "").strip()[:40]
    place = (d.get("place") or "").strip()[:80]
    dob = (d.get("dob") or "").strip()
    tob = (d.get("tob") or "").strip()
    relation = d.get("relation") or "self"
    gender = d.get("gender") or ""
    try:
        bd = datetime.strptime(dob, "%Y-%m-%d").date()
    except ValueError:
        return None, "Please enter a valid date of birth."
    if not (date(1900, 1, 1) <= bd <= datetime.now(timezone.utc).date()):
        return None, "Date of birth must be between 1900 and today."
    if tob:
        try:
            datetime.strptime(tob, "%H:%M")
        except ValueError:
            return None, "Please enter a valid time of birth."
    if len(place) < 2:
        return None, "Please enter the place of birth."
    if relation not in RELATIONS:
        return None, "Unknown relation."
    if gender not in GENDERS:
        gender = ""
    if not name and relation == "self":
        name = username.capitalize()
    return dict(name=name, place=place, dob=dob, tob=tob, relation=relation, gender=gender), None


def get_profile(uid, pid):
    rows = q("SELECT * FROM profiles WHERE id=:i AND user_id=:u", i=pid, u=uid)
    return rows[0] if rows else None


def user_profiles(uid):
    return q("SELECT * FROM profiles WHERE user_id=:u ORDER BY is_primary DESC, created_at", u=uid)


def kundli_for(p):
    try:
        return astro.build_kundli(p)
    except Exception as e:
        print("[astro] calculation failed:", e)
        return None


def public_profile(p, with_summary=True):
    out = {k: p[k] for k in ("id", "name", "relation", "gender", "dob", "tob", "place", "place_label")}
    out["is_primary"] = bool(p["is_primary"])
    out["located"] = p["lat"] is not None and bool(p["tz"])
    if with_summary:
        k = kundli_for(p)
        out["summary"] = k["summary"] if k else None
    return out


def pick_profile(uid, pid):
    rows = user_profiles(uid)
    if not rows:
        return None, []
    chosen = next((r for r in rows if r["id"] == pid), None) or next((r for r in rows if r["is_primary"]), rows[0])
    return chosen, [r for r in rows if r["id"] != chosen["id"]]


# ---------------- Pages & accounts ----------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")



# ---------------- Admin dashboard (only for usernames listed in ADMIN_USERS) ----------------
def admin_names():
    return {n.strip().lower() for n in (os.getenv("ADMIN_USERS") or "").split(",") if n.strip()}


def is_admin():
    return bool(session.get("name")) and session["name"].lower() in admin_names()


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not load_session():
            return jsonify(error="Please log in."), 401
        if not is_admin():
            return jsonify(error="Not allowed."), 403
        return f(*a, **k)
    return wrapper


def days_back(n):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def per_day(sql, days):
    rows = q(sql, since=days[0])
    found = {r["d"]: r["n"] for r in rows}
    return [{"day": d, "count": found.get(d, 0)} for d in days]


@app.get("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")


@app.get("/api/admin/overview")
@admin_required
def admin_overview():
    now = datetime.now(timezone.utc)
    iso = lambda delta: (now - delta).isoformat()
    one = lambda sql, **p: q(sql, **p)[0]["n"]
    days = days_back(14)

    totals = {
        "users": one("SELECT COUNT(*) AS n FROM users"),
        "new_today": one("SELECT COUNT(*) AS n FROM users WHERE created_at>=:t", t=days[-1]),
        "active_now": one("SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE last_seen>=:t", t=iso(timedelta(minutes=15))),
        "active_24h": one("SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE last_seen>=:t", t=iso(timedelta(hours=24))),
        "active_7d": one("SELECT COUNT(DISTINCT user_id) AS n FROM sessions WHERE last_seen>=:t", t=iso(timedelta(days=7))),
        "chats": one("SELECT COUNT(*) AS n FROM chats"),
        "questions": one("SELECT COUNT(*) AS n FROM messages WHERE role='user'"),
        "questions_today": one("SELECT COUNT(*) AS n FROM messages WHERE role='user' AND created_at>=:t", t=days[-1]),
        "kundlis": one("SELECT COUNT(*) AS n FROM profiles"),
    }
    signups = per_day("SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM users "
                      "WHERE created_at>=:since GROUP BY substr(created_at,1,10)", days)
    questions = per_day("SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM messages "
                        "WHERE role='user' AND created_at>=:since GROUP BY substr(created_at,1,10)", days)

    users = q("""SELECT u.id, u.username, u.created_at, u.last_login_at,
                   (SELECT COUNT(*) FROM chats c WHERE c.user_id=u.id) AS chats,
                   (SELECT COUNT(*) FROM messages m JOIN chats c ON c.id=m.chat_id
                      WHERE c.user_id=u.id AND m.role='user') AS questions,
                   (SELECT COUNT(*) FROM profiles p WHERE p.user_id=u.id) AS kundlis,
                   (SELECT MAX(s.last_seen) FROM sessions s WHERE s.user_id=u.id) AS last_seen,
                   (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id) AS devices
                 FROM users u ORDER BY u.created_at DESC LIMIT 500""")
    system = {
        "version": VERSION,
        "model": MODEL,
        "gemini_key": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
        "database": "sqlite" if db_url.startswith("sqlite") else "postgres",
        "astro_engine": bool(astro.AVAILABLE),
        "host": "vercel" if ON_VERCEL else ("render" if os.getenv("RENDER") else "local"),
    }
    return jsonify(totals=totals, signups=signups, questions=questions,
                   users=[dict(u) for u in users], system=system, brand=BRAND)


@app.delete("/api/admin/users/<uid>/sessions")
@admin_required
def admin_logout_user(uid):
    revoke_sessions(uid)
    return jsonify(ok=True)


@app.get("/api/health")
def health():
    try:
        db = {"db": "ok", "users": q("SELECT COUNT(*) AS n FROM users")[0]["n"]}
    except Exception as e:
        print("[health] database error:", str(e)[:300])      # full detail is in the Vercel logs
        db = {"db": "error", "db_error": type(e).__name__}
    return jsonify(ok=True, version=VERSION, astro=astro.AVAILABLE, models=MODELS[:1], **db)


def read_auth():
    d = request.get_json(silent=True) or {}
    return d, (d.get("username") or "").strip().lower(), str(d.get("password") or "")


@app.post("/api/signup")
def signup():
    d, u, p = read_auth()
    ip = client_ip()
    wait = t_wait([f"signup:ip:{ip}"])
    if wait:
        return jsonify(error=wait_msg(wait)), 429
    if not re.fullmatch(r"[a-z0-9_]{3,20}", u):
        return jsonify(error="Username: 3-20 letters, numbers or _ only."), 400
    if u in RESERVED_NAMES:
        return jsonify(error="That username is reserved. Please pick another."), 400
    err = password_error(p, u)
    if err:
        return jsonify(error=err), 400
    t_hit(f"signup:ip:{ip}", 8, 3600, 3600)       # max 8 new accounts per IP per hour
    uid = new_id()
    try:
        q("""INSERT INTO users (id, username, password_hash, created_at, pw_changed_at, last_login_at)
             VALUES (:i, :u, :h, :t, :t, :t)""", i=uid, u=u, h=generate_password_hash(p), t=now())
    except Exception:
        return jsonify(error="That username is already taken."), 409
    code = issue_recovery_code(uid)
    create_session(uid, u, bool(d.get("remember", True)))
    return jsonify(username=u, recovery_code=code, has_recovery=True)


@app.post("/api/login")
def login():
    d, u, p = read_auth()
    ip = client_ip()
    k_ui, k_ip, k_u = f"login:ui:{u}|{ip}", f"login:ip:{ip}", f"login:u:{u}"
    wait = t_wait([k_ui, k_ip, k_u])
    if wait:
        return jsonify(error=wait_msg(wait)), 429
    rows = q("SELECT id, password_hash, recovery_hash FROM users WHERE username=:u", u=u) if u else []
    ok = bool(rows) and len(p) <= 200 and check_password_hash(rows[0]["password_hash"], p)
    if not rows:
        check_password_hash(DUMMY_HASH, p[:200])      # same work for unknown users
    if not ok:
        t_hit(k_ui, 5, 900, 900)       # 5 wrong tries for this user from this IP -> 15 min lock
        t_hit(k_ip, 30, 900, 900)      # 30 wrong tries from one IP -> 15 min lock
        t_hit(k_u, 25, 3600, 3600)     # 25 wrong tries on one account from anywhere -> 1 hour lock
        return jsonify(error="Wrong username or password."), 401
    t_clear(k_ui, k_u)
    q("UPDATE users SET last_login_at=:t WHERE id=:i", t=now(), i=rows[0]["id"])
    create_session(rows[0]["id"], u, bool(d.get("remember", True)))
    return jsonify(username=u, has_recovery=bool(rows[0]["recovery_hash"]))


@app.post("/api/logout")
def logout():
    token = session.get("sid")
    if token:
        q("DELETE FROM sessions WHERE id=:i", i=hashlib.sha256(token.encode()).hexdigest())
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    r = q("SELECT recovery_hash, created_at, last_login_at FROM users WHERE id=:i", i=session["uid"])[0]
    return jsonify(username=session["name"], has_recovery=bool(r["recovery_hash"]),
                   created_at=r["created_at"], last_login_at=r["last_login_at"], is_admin=is_admin())


# ---------------- Forgot password (recovery code) ----------------
@app.post("/api/password/reset")
def password_reset():
    d = request.get_json(silent=True) or {}
    u = (d.get("username") or "").strip().lower()
    code, new_pw = norm_code(d.get("code")), str(d.get("new_password") or "")
    ip = client_ip()
    k_u, k_ip = f"reset:u:{u}", f"reset:ip:{ip}"
    wait = t_wait([k_u, k_ip])
    if wait:
        return jsonify(error=wait_msg(wait)), 429
    rows = q("SELECT id, recovery_hash FROM users WHERE username=:u", u=u) if u else []
    ok = bool(rows) and bool(rows[0]["recovery_hash"]) and len(code) == 16 \
        and check_password_hash(rows[0]["recovery_hash"], code)
    if not rows:
        check_password_hash(DUMMY_HASH, code)
    if not ok:
        t_hit(k_u, 5, 3600, 3600)
        t_hit(k_ip, 10, 3600, 3600)
        return jsonify(error="Username or recovery code is wrong."), 401
    err = password_error(new_pw, u)
    if err:
        return jsonify(error=err), 400            # code is not used up by a weak password
    uid = rows[0]["id"]
    q("UPDATE users SET password_hash=:h, pw_changed_at=:t, last_login_at=:t WHERE id=:i",
      h=generate_password_hash(new_pw), t=now(), i=uid)
    revoke_sessions(uid)                           # everyone is logged out
    t_clear(k_u, f"login:u:{u}", f"login:ui:{u}|{ip}")
    new_code = issue_recovery_code(uid)            # the used code is replaced
    create_session(uid, u, True)
    return jsonify(username=u, recovery_code=new_code, has_recovery=True)


# ---------------- Account & security ----------------
def verify_current_password(uid):
    """Shared by change-password / new-recovery-code / delete. Returns (user_row, error_response)."""
    d = request.get_json(silent=True) or {}
    k = f"pw:{uid}"
    wait = t_wait([k])
    if wait:
        return None, (jsonify(error=wait_msg(wait)), 429)
    row = q("SELECT id, username, password_hash FROM users WHERE id=:i", i=uid)[0]
    pw = str(d.get("password") or d.get("current") or "")
    if len(pw) > 200 or not check_password_hash(row["password_hash"], pw):
        t_hit(k, 5, 900, 900)
        return None, (jsonify(error="Current password is wrong."), 401)
    t_clear(k)
    return row, None


@app.post("/api/account/password")
@login_required
def change_password():
    uid = session["uid"]
    row, err = verify_current_password(uid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    new_pw = str(d.get("new") or "")
    msg = password_error(new_pw, row["username"])
    if msg:
        return jsonify(error=msg), 400
    if new_pw == str(d.get("current") or ""):
        return jsonify(error="New password must be different from the current one."), 400
    q("UPDATE users SET password_hash=:h, pw_changed_at=:t WHERE id=:i",
      h=generate_password_hash(new_pw), t=now(), i=uid)
    revoke_sessions(uid, keep_id=g.sess["id"])     # log out every other device
    return jsonify(ok=True, message="Password changed. Other devices were logged out.")


@app.post("/api/account/recovery")
@login_required
def new_recovery():
    row, err = verify_current_password(session["uid"])
    if err:
        return err
    return jsonify(recovery_code=issue_recovery_code(row["id"]))


@app.get("/api/account/sessions")
@login_required
def list_sessions():
    rows = q("""SELECT id, public_id, device, ip, created_at, last_seen FROM sessions
                WHERE user_id=:u AND expires_at>:n ORDER BY last_seen DESC""",
             u=session["uid"], n=now())
    return jsonify(sessions=[{"id": r["public_id"], "device": r["device"], "ip": r["ip"],
                              "created_at": r["created_at"], "last_seen": r["last_seen"],
                              "current": r["id"] == g.sess["id"]} for r in rows])


@app.post("/api/account/sessions/revoke-others")
@login_required
def revoke_others():
    revoke_sessions(session["uid"], keep_id=g.sess["id"])
    return jsonify(ok=True)


@app.delete("/api/account/sessions/<pid>")
@login_required
def revoke_one(pid):
    q("DELETE FROM sessions WHERE public_id=:p AND user_id=:u", p=pid, u=session["uid"])
    return jsonify(ok=True)


@app.delete("/api/account")
@login_required
def delete_account():
    uid = session["uid"]
    row, err = verify_current_password(uid)
    if err:
        return err
    q("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE user_id=:u)", u=uid)
    for table in ("chats", "profiles", "memory", "sessions"):
        q(f"DELETE FROM {table} WHERE user_id=:u", u=uid)
    q("DELETE FROM users WHERE id=:u", u=uid)
    session.clear()
    return jsonify(ok=True)


# ---------------- Profiles API ----------------
@app.get("/api/profiles")
@login_required
def list_profiles():
    return jsonify(profiles=[public_profile(p) for p in user_profiles(session["uid"])])


@app.post("/api/profiles")
@login_required
def create_profile():
    uid = session["uid"]
    existing = q("SELECT COUNT(*) AS n FROM profiles WHERE user_id=:u", u=uid)[0]["n"]
    if existing >= MAX_PROFILES:
        return jsonify(error=f"You can save up to {MAX_PROFILES} people."), 400
    data, err = clean_profile(request.get_json(silent=True) or {}, session["name"])
    if err:
        return jsonify(error=err), 400
    geo = astro.geocode(data["place"])
    pid, t = new_id(), now()
    q("INSERT INTO profiles (id, user_id, name, relation, gender, dob, tob, place, place_label, lat, lon, tz, "
      "is_primary, created_at, updated_at) VALUES (:i, :u, :name, :relation, :gender, :dob, :tob, :place, "
      ":label, :lat, :lon, :tz, :prim, :t, :t)",
      i=pid, u=uid, label=geo["label"] if geo else "", lat=geo["lat"] if geo else None,
      lon=geo["lon"] if geo else None, tz=geo["tz"] if geo else "",
      prim=1 if existing == 0 else 0, t=t, **data)
    return jsonify(profile=public_profile(get_profile(uid, pid)), warning=None if geo else PLACE_WARN)


@app.put("/api/profiles/<pid>")
@login_required
def update_profile(pid):
    uid = session["uid"]
    cur = get_profile(uid, pid)
    if not cur:
        return jsonify(error="Profile not found."), 404
    data, err = clean_profile(request.get_json(silent=True) or {}, session["name"])
    if err:
        return jsonify(error=err), 400
    if data["place"].lower() == cur["place"].lower() and cur["lat"] is not None and cur["tz"]:
        geo = {"lat": cur["lat"], "lon": cur["lon"], "tz": cur["tz"], "label": cur["place_label"]}
    else:
        geo = astro.geocode(data["place"])
    q("UPDATE profiles SET name=:name, relation=:relation, gender=:gender, dob=:dob, tob=:tob, place=:place, "
      "place_label=:label, lat=:lat, lon=:lon, tz=:tz, updated_at=:t WHERE id=:i AND user_id=:u",
      i=pid, u=uid, label=geo["label"] if geo else "", lat=geo["lat"] if geo else None,
      lon=geo["lon"] if geo else None, tz=geo["tz"] if geo else "", t=now(), **data)
    return jsonify(profile=public_profile(get_profile(uid, pid)), warning=None if geo else PLACE_WARN)


@app.delete("/api/profiles/<pid>")
@login_required
def delete_profile(pid):
    uid = session["uid"]
    cur = get_profile(uid, pid)
    if cur:
        q("DELETE FROM profiles WHERE id=:i AND user_id=:u", i=pid, u=uid)
        if cur["is_primary"]:
            rest = user_profiles(uid)
            if rest:
                q("UPDATE profiles SET is_primary=1 WHERE id=:i AND user_id=:u", i=rest[0]["id"], u=uid)
    return jsonify(ok=True)


@app.post("/api/profiles/<pid>/primary")
@login_required
def make_primary(pid):
    uid = session["uid"]
    if not get_profile(uid, pid):
        return jsonify(error="Profile not found."), 404
    q("UPDATE profiles SET is_primary=0 WHERE user_id=:u", u=uid)
    q("UPDATE profiles SET is_primary=1 WHERE id=:i AND user_id=:u", i=pid, u=uid)
    return jsonify(ok=True)


@app.get("/api/profiles/<pid>/kundli")
@login_required
def profile_kundli(pid):
    p = get_profile(session["uid"], pid)
    if not p:
        return jsonify(error="Profile not found."), 404
    if not astro.AVAILABLE:
        return jsonify(error="The chart engine is not installed on the server yet."), 503
    if p["lat"] is None or not p["tz"]:
        return jsonify(error="Baba could not find the birth place on the map. Edit the place "
                             "(City, State, Country) and save again."), 422
    k = kundli_for(p)
    if not k:
        return jsonify(error="Could not calculate this kundli. Please check the birth details."), 500
    return jsonify(k)


# ---------------- Memory API ----------------
@app.get("/api/memory")
@login_required
def memory_get():
    row = get_memory_row(session["uid"])
    return jsonify(summary=row["summary"] if row else "", enabled=bool(row["enabled"]) if row else True)


@app.put("/api/memory")
@login_required
def memory_put():
    d = request.get_json(silent=True) or {}
    save_memory(session["uid"], summary=(d.get("summary") or "").strip()[:800],
                enabled=bool(d.get("enabled", True)))
    return jsonify(ok=True)


@app.post("/api/memory/refresh")
@login_required
def memory_refresh():
    d = request.get_json(silent=True) or {}
    cid, uid = d.get("chat_id"), session["uid"]
    if not cid or not q("SELECT id FROM chats WHERE id=:c AND user_id=:u", c=cid, u=uid):
        return jsonify(ok=False), 404
    rows = q("SELECT role, content FROM messages WHERE chat_id=:c ORDER BY created_at DESC LIMIT 2", c=cid)
    user_msg = next((r["content"] for r in rows if r["role"] == "user"), "")
    reply = next((r["content"] for r in rows if r["role"] == "model"), "")
    if user_msg and reply:
        update_memory(uid, user_msg, reply)
    return jsonify(ok=True)


@app.delete("/api/memory")
@login_required
def memory_clear():
    save_memory(session["uid"], summary="")
    return jsonify(ok=True)


# ---------------- Chats (every query is filtered by the logged-in user) ----------------
@app.get("/api/chats")
@login_required
def list_chats():
    rows = q("SELECT id, title, updated_at FROM chats WHERE user_id=:u ORDER BY updated_at DESC LIMIT 100",
             u=session["uid"])
    return jsonify(chats=rows)


@app.get("/api/chats/<cid>")
@login_required
def get_chat(cid):
    if not q("SELECT id FROM chats WHERE id=:c AND user_id=:u", c=cid, u=session["uid"]):
        return jsonify(error="Chat not found."), 404
    rows = q("SELECT role, content, created_at FROM messages WHERE chat_id=:c ORDER BY created_at", c=cid)
    return jsonify(messages=rows)


@app.delete("/api/chats/<cid>")
@login_required
def delete_chat(cid):
    if q("SELECT id FROM chats WHERE id=:c AND user_id=:u", c=cid, u=session["uid"]):
        q("DELETE FROM messages WHERE chat_id=:c", c=cid)
        q("DELETE FROM chats WHERE id=:c AND user_id=:u", c=cid, u=session["uid"])
    return jsonify(ok=True)


@app.post("/api/chat")
@login_required
def api_chat():
    d = request.get_json(silent=True) or {}
    message = (d.get("message") or "").strip()[:2000]
    language = d.get("language", "Hinglish")
    topic = d.get("topic") if d.get("topic") in FOCUS_GUIDES else "general"
    cid, uid = d.get("chat_id"), session["uid"]
    if not message:
        return jsonify(reply="Please type something for Baba.", kind="error"), 400

    history = []
    if cid:
        if not q("SELECT id FROM chats WHERE id=:c AND user_id=:u", c=cid, u=uid):
            return jsonify(reply="Chat not found.", kind="error"), 404
        rows = q("SELECT role, content FROM messages WHERE chat_id=:c ORDER BY created_at DESC LIMIT :n",
                 c=cid, n=MAX_MESSAGES)[::-1]
        history = [{"role": r["role"], "parts": [{"text": r["content"]}]} for r in rows]

    # Saved birth profile -> real chart facts for Baba
    profile_text = None
    chosen, others = pick_profile(uid, d.get("profile_id"))
    if chosen:
        try:
            profile_text = astro.describe(
                chosen, kundli_for(chosen), [(o, kundli_for(o)) for o in others])
        except Exception as e:
            print("[astro] describe failed:", e)
    mem = get_memory_row(uid)
    memory_text = mem["summary"] if mem and mem["enabled"] and mem["summary"] else None

    system = build_system(language, topic, profile_text, memory_text)
    contents = history + [{"role": "user", "parts": [{"text": message}]}]

    t_user = now()
    raw, kind, err = call_model(contents, system)
    if kind:
        return jsonify(reply=err, kind=kind, chat_id=cid), (503 if kind == "busy" else 500)
    reply, suggestions = split_followups(raw)

    if not cid:
        cid = new_id()
        title = (d.get("title") or message.splitlines()[0])[:40]
        q("INSERT INTO chats (id, user_id, title, updated_at) VALUES (:c, :u, :t, :n)",
          c=cid, u=uid, t=title, n=t_user)
    q("INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (:i, :c, 'user', :m, :t)",
      i=new_id(), c=cid, m=message, t=t_user)
    t_reply = now()
    q("INSERT INTO messages (id, chat_id, role, content, created_at) VALUES (:i, :c, 'model', :m, :t)",
      i=new_id(), c=cid, m=reply, t=t_reply)
    q("UPDATE chats SET updated_at=:n WHERE id=:c AND user_id=:u", n=t_reply, c=cid, u=uid)

    # update Baba's memory in the background (1st message, then every 3rd) to save quota
    n_user = q("SELECT COUNT(*) AS n FROM messages WHERE chat_id=:c AND role='user'", c=cid)[0]["n"]
    memory_due = n_user == 1 or n_user % 3 == 0
    if memory_due and not ON_VERCEL:
        remember_async(uid, message, reply)
    # On Vercel the browser asks for the memory update in a separate request (/api/memory/refresh),
    # so the chat reply is not delayed by it.
    return jsonify(reply=reply, kind=None, chat_id=cid, suggestions=suggestions,
                   memory_due=bool(memory_due and ON_VERCEL))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)    
