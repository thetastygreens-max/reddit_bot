[README.md](https://github.com/user-attachments/files/30780603/README.md)
# Reddit Video Editor Lead Bot (RSS-based)

Monitors Reddit for people looking to hire a video editor, filters posts with Claude,
and sends you a Telegram alert in real time.

This version reads each subreddit's **public RSS feed** instead of Reddit's official
API — no Reddit app registration or approval needed. Reddit recently tightened its
API app-creation process (Responsible Builder Policy), so RSS is the fastest path
to something working today.

## 1. Get your credentials

### Telegram bot
1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts
2. Copy the **bot token** it gives you
3. Send your new bot any message
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
5. Copy the `chat.id` value from the JSON response — that's your chat ID

### Anthropic API key
Get one from https://console.anthropic.com/settings/keys

(No Reddit credentials needed for this version.)

## 2. Configure environment variables

Set these (locally in a `.env` file, or in Railway's Variables tab):

```
TELEGRAM_BOT_TOKEN=xxxxx
TELEGRAM_CHAT_ID=xxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxx
SUBREDDITS=forhire,slavelabour,videography,editors,editingrequests,VideoEditing
POLL_INTERVAL_SECONDS=120
```

`SUBREDDITS` is comma-separated (no spaces), unlike the old `+`-joined format.

## 3. Deploy on Railway (free tier)

1. Push these files (`main.py`, `requirements.txt`) to a new GitHub repo
2. Go to https://railway.app, sign in with GitHub
3. "New Project" → "Deploy from GitHub repo" → select your repo
4. In the project's **Variables** tab, add all the environment variables above
5. Railway auto-detects Python and runs `python main.py` — if not, set the
   start command manually in Settings → Deploy
6. Once deployed, check the **Logs** tab — you should see
   "Monitoring (RSS): forhire, slavelabour, ..." and per-post scan results

The bot polls each subreddit's RSS feed every 2 minutes by default (adjust
`POLL_INTERVAL_SECONDS`). It keeps a small SQLite file (`seen_posts.db`) so it
never alerts you twice about the same post — note this resets if Railway
redeploys/restarts your service on the free tier, since the filesystem isn't
persistent long-term. For fully persistent dedup, consider Railway's volume
feature or an external key-value store later.

## 4. Test locally first (optional)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export ANTHROPIC_API_KEY=...
python main.py
```

## Notes & limits

- RSS feeds only show the newest ~25 posts per subreddit — fine for near
  real-time monitoring at a 1-2 minute poll interval, but you could miss
  posts if a subreddit is extremely high-volume and you poll too rarely.
- Reddit may rate-limit or occasionally block requests without a browser-like
  User-Agent; the script already sets one, but if you see repeated fetch
  errors in the logs, slow down `POLL_INTERVAL_SECONDS`.
- If you later want full API access (search, historical posts, higher
  reliability), you can register through Reddit's newer Developer Platform at
  https://developers.reddit.com/app-registration, or file a ticket if your
  use case doesn't fit their Devvit app model.

## Tuning

- Add/remove subreddits in `SUBREDDITS`
- Adjust the confidence threshold in `main.py` (`confidence") >= 60`) —
  lower it to catch more (with more false positives), raise it to be stricter
- Edit the prompt inside `is_video_editor_lead()` to change what counts as a lead
