import os
import time
import json
import sqlite3
import requests
import feedparser

# ---- Config from environment variables ----
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

SUBREDDITS = os.environ.get(
    "SUBREDDITS",
    "forhire,slavelabour,videography,editors,editingrequests,VideoEditing"
).split(",")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
DB_PATH = os.environ.get("DB_PATH", "seen_posts.db")

# Safety valve so a big backlog (first run, or a wiped DB) can't blow through
# your Gemini rate limit in one cycle. Only this many posts get AI-screened
# per poll; the rest are just marked seen and picked up as "new" never again
# (they'll simply be skipped, not screened later).
MAX_AI_CALLS_PER_CYCLE = int(os.environ.get("MAX_AI_CALLS_PER_CYCLE", "5"))

# On the very first run (empty DB), the whole current backlog looks "new."
# Screening all of it with Gemini immediately is what blows the rate limit
# before you've even confirmed the pipeline works. If true (default),
# the first cycle only marks posts as seen -- no Gemini calls, no alerts --
# so testing starts clean and only genuinely new posts going forward get screened.
BACKFILL_WITHOUT_SCREENING = os.environ.get("BACKFILL_WITHOUT_SCREENING", "true").lower() == "true"

# Reddit blocks requests without a real-looking User-Agent
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; editor-lead-bot/1.0)"}

# Combine all subreddits into one multi-reddit feed URL, e.g. r/a+b+c/new/.rss
# This means ONE request per poll cycle instead of one per subreddit,
# which avoids Reddit's per-IP rate limiting (429 errors).
MULTIREDDIT = "+".join(s.strip() for s in SUBREDDITS)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
# Auth (AQ.*) keys must be sent as a header, not a ?key= query param.
# Standard (AIzaSy...) keys also work fine with this header, so this
# is the safe, forward-compatible way to authenticate either type.
GEMINI_HEADERS = {
    "Content-Type": "application/json",
    "x-goog-api-key": GEMINI_API_KEY,
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def db_is_empty(conn):
    cur = conn.execute("SELECT 1 FROM seen LIMIT 1")
    return cur.fetchone() is None


def already_seen(conn, post_id):
    cur = conn.execute("SELECT 1 FROM seen WHERE id = ?", (post_id,))
    return cur.fetchone() is not None


def mark_seen(conn, post_id):
    conn.execute("INSERT OR IGNORE INTO seen (id) VALUES (?)", (post_id,))
    conn.commit()


def fetch_new_posts(max_retries=3):
    """Fetch newest posts from all subreddits in a single combined RSS request.
    Retries with backoff if Reddit returns 429 (Too Many Requests)."""
    url = f"https://www.reddit.com/r/{MULTIREDDIT}/new/.rss"

    for attempt in range(max_retries):
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 30)) * (attempt + 1)
            print(f"Rate limited (429). Waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        print("Gave up after repeated 429s this cycle.")
        return []

    feed = feedparser.parse(resp.content)
    posts = []
    for entry in feed.entries:
        # entry.link looks like https://www.reddit.com/r/forhire/comments/xyz/...
        subreddit = entry.link.split("/r/")[1].split("/")[0] if "/r/" in entry.link else "unknown"
        posts.append({
            "id": entry.get("id", entry.get("link")),
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "content": entry.get("content", [{}])[0].get("value", "") if entry.get("content") else entry.get("summary", ""),
            "subreddit": subreddit,
            "author": entry.get("author", "unknown"),
        })
    return posts


def is_video_editor_lead(title: str, body: str, max_retries=3) -> dict:
    """Ask Gemini whether this post is someone hiring/looking for a video editor."""
    
    # Safely escape text to prevent unescaped quotes/newlines from breaking the JSON payload
    clean_title = json.dumps(title)
    clean_body = json.dumps(body[:1500])

    prompt = f"""You are screening Reddit posts to find people who are HIRING or LOOKING FOR a video editor (freelance gig, paid job, or collaboration).

Post title: {clean_title}
Post body: {clean_body}

Determine if this post is a hiring/lead post for a video editor.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 200,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "is_lead": {"type": "BOOLEAN"},
                    "confidence": {"type": "INTEGER"},
                    "reason": {"type": "STRING"}
                },
                "required": ["is_lead", "confidence", "reason"]
            }
        },
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(GEMINI_URL, headers=GEMINI_HEADERS, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"Gemini rate limited (429). Waiting {wait}s...")
                time.sleep(wait)
                continue
            
            if resp.status_code != 200:
                print(f"Gemini API Error [{resp.status_code}]: {resp.text}")
                return {"is_lead": False, "confidence": 0, "reason": f"http_{resp.status_code}"}

            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(text)
        except Exception as e:
            print(f"Gemini call failed: {e}")
            return {"is_lead": False, "confidence": 0, "reason": "api_error"}

    print("Gave up on this post after repeated Gemini 429s.")
    return {"is_lead": False, "confidence": 0, "reason": "rate_limited"}
import datetime

def send_telegram_message(text: str):
    """Utility to send any text message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })

def send_telegram_alert(post):
    """Alert for a high-confidence video editing lead."""
    message = (
        f"🎬 *New video editor lead!*\n\n"
        f"*{post['title']}*\n\n"
        f"r/{post['subreddit']} | u/{post['author']}\n"
        f"{post['link']}"
    )
    send_telegram_message(message)

def main_loop():
    conn = init_db()
    print(f"Monitoring (RSS, combined): {MULTIREDDIT}")

    # Buffer for posts evaluated in the last 5 minutes:
    # stores dicts of {"title": str, "subreddit": str, "timestamp": float}
    scanned_recent_posts = []

    first_cycle = db_is_empty(conn)
    if first_cycle and BACKFILL_WITHOUT_SCREENING:
        print("Empty DB detected -> backfilling current backlog without AI "
              "screening (set BACKFILL_WITHOUT_SCREENING=false to disable).")

    while True:
        try:
            posts = fetch_new_posts()
        except Exception as e:
            print(f"Error fetching combined feed: {e}")
            posts = []

        new_posts = [p for p in posts if not already_seen(conn, p["id"])]

        if not posts:
            print("No entries returned from feed this cycle.")
        elif not new_posts:
            print(f"Fetched {len(posts)} posts, 0 new (all already seen).")

        if first_cycle and BACKFILL_WITHOUT_SCREENING:
            for post in new_posts:
                mark_seen(conn, post["id"])
            print(f"Backfilled {len(new_posts)} existing posts as seen (0 Gemini calls used).")
            first_cycle = False
        else:
            screened = 0
            found_lead_this_cycle = False

            for post in new_posts:
                mark_seen(conn, post["id"])

                if screened >= MAX_AI_CALLS_PER_CYCLE:
                    print(f"Hit MAX_AI_CALLS_PER_CYCLE ({MAX_AI_CALLS_PER_CYCLE}); skipping rest.")
                    break

                result = is_video_editor_lead(post["title"], post["content"])
                print(f"[r/{post['subreddit']}] {post['title'][:60]} -> {result}")
                screened += 1

                # Track this scanned post timestamp for 5-min history
                scanned_recent_posts.append({
                    "title": post["title"],
                    "subreddit": post["subreddit"],
                    "timestamp": time.time()
                })

                if result.get("is_lead") and result.get("confidence", 0) >= 60:
                    found_lead_this_cycle = True
                    send_telegram_alert(post)

                time.sleep(6)

            # Purge entries older than 5 minutes (300 seconds)
            cutoff_time = time.time() - 300
            scanned_recent_posts = [
                p for p in scanned_recent_posts if p["timestamp"] >= cutoff_time
            ]

            # Test notification: Send status update if no lead was found
            if not found_lead_this_cycle:
                if scanned_recent_posts:
                    post_list = "\n".join([
                        f"• `[r/{p['subreddit']}]` {p['title'][:50]}" 
                        for p in scanned_recent_posts
                    ])
                    summary_msg = (
                        "scanned these leads but none found to be worth it:\n\n"
                        f"{post_list}"
                    )
                else:
                    summary_msg = "scanned these leads but none found to be worth it (0 new posts evaluated in last 5 min)."

                # Enforce Telegram 4096 character limit safety margin
                if len(summary_msg) > 4000:
                    summary_msg = summary_msg[:3990] + "\n..."

                send_telegram_message(summary_msg)

        print(f"Cycle done. Sleeping {POLL_INTERVAL_SECONDS}s until next poll...")
        time.sleep(POLL_INTERVAL_SECONDS)
