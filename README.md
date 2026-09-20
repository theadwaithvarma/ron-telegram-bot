<div align="center">

# 🤖 Ron

**Your conversational AI companion on Telegram**

*A self-sufficient AI companion on Telegram — multi-provider brain, token-smart, and self-healing.*

[![Telegram](https://img.shields.io/badge/Telegram-@ronrebootedbot-2CA5E0?style=flat-square&logo=telegram)](https://t.me/ronrebootedbot)
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![AI](https://img.shields.io/badge/AI-Multi--provider_failover-8E44AD?style=flat-square)]()
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Live-success?style=flat-square)]()

</div>

---

## ✨ What is Ron?

Ron is a **full-featured conversational Telegram AI bot** that feels less like a script and more like a sharp friend who happens to live in your chats. It answers DMs from anyone, politely stays quiet in groups until mentioned, streams long answers token-by-token as you watch, and doubles as a group-management power tool with native polls, quizzes, and forum-topic controls.

---

## 🚀 Features

### 💬 Conversational AI
- **Multi-provider brain** (`ron_ai.py`) with automatic failover: Groq → Gemini → OpenRouter, each with its own model ladder, tunable in `ron_ai.json`
- Per-provider rate-limit cooldowns, dead-key detection, and silent skipping of providers you haven't keyed — runs fine on Groq alone
- Per-chat rolling conversation history, so Ron remembers context
- Provider-neutral errors for users; the owner gets a private heads-up if a key dies
- Clean Telegram Markdown formatting, never raw artifact soup

### 🪙 Token Economy
- Greetings and small talk route to each provider's smallest model with a short prompt
- Pure acknowledgements (`ok`, `lol`, `thanks`, `👍`) get instant canned replies — zero API calls
- A failed stream never re-runs the whole provider chain; you get the honest error instead of a double token burn

### 🩺 Self-Healing
- **Watchdog supervisor** (`ron_watchdog.py`): 60-second checks for dead or hung daemons, duplicate processes, and broken configs — with exponential restart backoff and last-known-good config restore (see `ron-watchdog.service.example` for systemd)
- **Degraded mode**: if every provider keeps failing, Ron queues messages to disk, tells users honestly, and replays everything on recovery — no silent drops, no duplicates

### ⚡ Live Streaming Replies
- Long answers **stream in real time** into 1:1 chats via Telegram's native message drafts (`sendMessageDraft`), with an instant "Thinking…" placeholder and throttled draft growth
- Native stop control (`can_stop` / `keep_on_stop`) with graceful fallback, honoring Telegram 10.3's `stopped_message_generation` — the stop button actually stops Ron mid-reply
- The finished reply is delivered as a rich message (`sendRichMessage`) with full formatting

### 📊 Native Polls & Quizzes
| Command | Usage |
|---------|-------|
| `/poll` | `/poll Question? \| Option A \| Option B` |
| `/pollm` | Multi-answer poll — `/pollm Q? \| A \| B \| C` |
| `/quiz` | Quiz mode — `/quiz Q? \| *Correct \| Wrong \| Wrong` |

### 🛠️ Group Power Tools
- **DM-to-group pings** — `[send_group: Group Name | message]` posts your message in a group, mentioning you (fuzzy group-name resolution)
- **Forum topic management** — detects forum status via cached `getChat`, auto-purges stale topics when a group disables them
- `[topic: ...]` and `[send_topic: ...]` trailers for topic actions, with explicit confirmation before destructive deletes
- **Quick-reply buttons** — `[buttons: Label | Label]` renders inline keyboards; tapping a button re-sends its label as your message
- **Contextual links** — `[links: Label -> https://...]` for clean inline references

### 👋 Ephemeral Welcomes
New joiners get greeted **privately** — zero group spam, full warmth.

### 💼 Business / Secretary Mode
Handles `business_connection` and `business_message` updates — Ron can answer customers through your Telegram Business account as one rich reply.

### 🔧 Reliability Details
- Typing indicators so you know Ron is thinking
- Graceful shutdown that drains in-flight replies before exiting
- Update-offset persistence — no re-processing on restart
- Heartbeat file so the watchdog can spot a hung process
- Provider-neutral rate-limit replies instead of generic error text

---

## 🏁 Quick Start

```bash
git clone https://github.com/theadwaithvarma/ron-telegram-bot.git
cd ron-telegram-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in BOT_TOKEN and GROQ_API_KEY (Gemini/OpenRouter optional)
python3 ron_daemon.py
```

That's it — Ron is alive. 🟢

For self-healing on a server, also run the watchdog (see `ron-watchdog.service.example`):

```bash
python3 ron_watchdog.py --loop   # or install the systemd unit

---

## ⚙️ Configuration

Everything comes from **environment variables** (see `.env.example`). A `.env` file next to the scripts loads automatically; shell-exported variables take precedence.

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY` | ✅ | Groq Cloud API key from [console.groq.com](https://console.groq.com) |
| `GEMINI_API_KEY` | — | Google AI Studio key — enables the Gemini failover rung |
| `OPENROUTER_API_KEY` | — | OpenRouter key — enables the OpenRouter failover rung (free models) |
| `GROQ_MODEL` | — | Chat model (default `openai/gpt-oss-120b`) |
| `GROQ_BASE_URL` | — | API base URL (default: Groq OpenAI-compatible) |
| `BOT_USERNAME` | — | Bot username without `@` (default `ronrebootedbot`) |
| `OWNER_ID` | — | Your Telegram user id — gets private alerts (e.g. dead API key) |
| `SYSTEM_PROMPT` | — | Override the bot persona |
| `RON_BOT_DIR` | — | Watchdog only: bot install dir (default: watchdog's own directory) |

The failover chain itself (providers and their model ladders) lives in `ron_ai.json` — edit it to re-tune without touching code.

Runtime state files (`ron_daemon.offset`, `ron_ai_history.json`, `ron_heartbeat.json`, `degraded_queue.jsonl`, `known_groups.json`, `known_topics.json`, `business_connections.json`) live next to the scripts and are git-ignored — never committed.

---

## 📁 Project Layout

```
ron-telegram-bot/
├── ron_daemon.py      # Long-polling daemon — routing, commands, trailer handlers, degraded mode
├── ron_ai.py          # Multi-provider chat backend — failover, cheap routing, history
├── ron_ai.json        # Provider/model chain config (no secrets)
├── ron_watchdog.py    # Supervisor — restarts dead/hung daemons, repairs broken configs
├── ron-watchdog.service.example  # systemd unit template for the watchdog
├── config.py          # Env-var config + .env loader (stdlib only)
├── stream_send.py     # Progressive draft streaming for 1:1 chats
├── rich_send.py       # Native rich message sender with plain-text fallback
├── ephemeral_send.py  # Private ephemeral messages (welcomes)
├── delete_messages.py # Message deletion helper
├── requirements.txt   # stdlib only — no third-party deps (Python 3.8+)
├── .env.example       # Template for required/optional config
├── CHANGELOG.md       # Version history
└── VERSION            # Current version (1.1.0)
```

---

## 🤝 Behavior

- **DMs:** open to anyone — Ron chats freely
- **Groups:** replies only when mentioned or replied to — no noise, no unwanted reactions
- **Forums:** topic-aware; topic trailers are only honored where topics exist

---

## 📜 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

*Built with ❤️ by [Adwaith Varma](https://github.com/theadwaithvarma)*

</div>
