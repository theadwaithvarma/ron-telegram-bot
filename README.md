<div align="center">

# 🤖 Ron

**Your conversational AI companion on Telegram**

*Powered by Groq's blazing-fast LLM inference — warm, witty, and always on duty.*

[![Telegram](https://img.shields.io/badge/Telegram-@ronrebootedbot-2CA5E0?style=flat-square&logo=telegram)](https://t.me/ronrebootedbot)
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Groq](https://img.shields.io/badge/Groq-gpt--oss--120b-F55036?style=flat-square)](https://console.groq.com/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Live-success?style=flat-square)]()

</div>

---

## ✨ What is Ron?

Ron is a **full-featured conversational Telegram AI bot** that feels less like a script and more like a sharp friend who happens to live in your chats. It answers DMs from anyone, politely stays quiet in groups until mentioned, streams long answers token-by-token as you watch, and doubles as a group-management power tool with native polls, quizzes, and forum-topic controls.

---

## 🚀 Features

### 💬 Conversational AI
- Groq-powered chat backend (`ron_ai.py`) — fast, natural replies with a warm tough-love-mentor personality
- Per-chat rolling conversation history, so Ron remembers context
- Clean Telegram Markdown formatting, never raw artifact soup

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

### 🔧 Reliability Details
- Typing indicators so you know Ron is thinking
- Graceful shutdown that drains in-flight replies before exiting
- Update-offset persistence — no re-processing on restart
- Honest `429 rate limited` replies instead of generic error text

---

## 🏁 Quick Start

```bash
git clone https://github.com/theadwaithvarma/ron-telegram-bot.git
cd ron-telegram-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in BOT_TOKEN and GROQ_API_KEY
python3 ron_daemon.py
```

That's it — Ron is alive. 🟢

---

## ⚙️ Configuration

Everything comes from **environment variables** (see `.env.example`). A `.env` file next to the scripts loads automatically; shell-exported variables take precedence.

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY` | ✅ | Groq Cloud API key from [console.groq.com](https://console.groq.com) |
| `GROQ_MODEL` | — | Chat model (default `openai/gpt-oss-120b`) |
| `GROQ_BASE_URL` | — | API base URL (default: Groq OpenAI-compatible) |
| `BOT_USERNAME` | — | Bot username without `@` (default `ronrebootedbot`) |
| `OWNER_ID` | — | Your Telegram user id (reserved, currently unused) |
| `SYSTEM_PROMPT` | — | Override the bot persona |

Runtime state files (`ron_daemon.offset`, `ron_ai_history.json`, `known_groups.json`, `known_topics.json`) live next to the scripts and are git-ignored — never committed.

---

## 📁 Project Layout

```
ron-telegram-bot/
├── ron_daemon.py      # Long-polling daemon — routing, commands, trailer handlers
├── ron_ai.py          # Groq chat backend — history, model fallback on 429s
├── config.py          # Env-var config + .env loader (stdlib only)
├── stream_send.py     # Progressive draft streaming for 1:1 chats
├── rich_send.py       # Native rich message sender with plain-text fallback
├── ephemeral_send.py  # Private ephemeral messages (welcomes)
├── delete_messages.py # Message deletion helper
├── requirements.txt   # stdlib only — no third-party deps (Python 3.8+)
├── .env.example       # Template for required/optional config
├── CHANGELOG.md       # Version history
└── VERSION            # Current version (1.0.0)
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
