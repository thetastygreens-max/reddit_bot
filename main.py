import os
import time
import json
import sqlite3
import requests
import feedparser
from anthropic import Anthropic

# ---- Config from environment variables ----
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

SUBREDDITS = os.environ.get(
    "SUBREDDITS",
    "forhire,slavelabour,videography,editors,editingrequests,VideoEditing"
).split(",")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
DB_PATH = os.environ.get("DB_PATH", "seen_posts.db")

# Reddit blocks requests without a real-looking User-Agent
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; editor-lead-bot/1.0)"}

claude = Anthropic(api_key=ANTHROPIC_API_KEY)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def already_seen(conn, post_id):
    cur = conn.execute("SELECT 1 FROM seen WHERE id = ?", (post_id,))
    return cur.fetchone() is not None


def mark_seen(conn, post_id):
    conn.execute("INSERT OR IGNORE INTO seen (id) VALUES (?)", (post_id,))
    conn.commit()


def fetch_new_posts(subreddit):
    """Fetch newest posts from a subreddit's public RSS feed."""
    url = f"https://www.reddit.com/r/{subreddit.strip()}/new/.rss"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    posts = []
    for entry in feed.entries:
        posts.append({
            "id": entry.get("id", entry.get("link")),
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "content": entry.get("content", [{}])[0].get("value", "") if entry.get("content") else entry.get("summary", ""),
            "subreddit": subreddit,
            "author": entry.get("author", "unknown"),
        })
    return posts


def is_video_editor_lead(title: str, body: str) -> dict:
    """Ask Claude whether this post is someone hiring/looking for a video editor."""
    prompt = f"""You are screening Reddit posts to find people who are HIRING or LOOKING FOR a video editor (freelance gig, paid job, or collaboration).

Post title: {title}
Post body (may include HTML): {body[:2000]}

Reply with ONLY a JSON object, no other text, in this exact format:
{{"is_lead": true or false, "confidence": 0-100, "reason": "one short sentence why"}}
"""
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"is_lead": False, "confidence": 0, "reason": "parse_error"}


def send_telegram_alert(post):
    message = (
        f"🎬 *New video editor lead!*\n\n"
        f"*{post['title']}*\n\n"
        f"r/{post['subreddit']} | u/{post['author']}\n"
        f"{post['link']}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    })


def main_loop():
    conn = init_db()
    print(f"Monitoring (RSS): {', '.join(SUBREDDITS)}")

    while True:
        for subreddit in SUBREDDITS:
            try:
                posts = fetch_new_posts(subreddit)
            except Exception as e:
                print(f"Error fetching r/{subreddit}: {e}")
                continue

            for post in posts:
                if already_seen(conn, post["id"]):
                    continue
                mark_seen(conn, post["id"])

                result = is_video_editor_lead(post["title"], post["content"])
                print(f"[r/{subreddit}] {post['title'][:60]} -> {result}")

                if result.get("is_lead") and result.get("confidence", 0) >= 60:
                    send_telegram_alert(post)

            # small delay between subreddits to be polite to reddit.com
            time.sleep(3)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
