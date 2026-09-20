# Ron — Telegram AI Bot

Conversational Telegram bot powered by Groq LLMs. DMs anyone, replies in groups
only when mentioned or replied to, streams long answers live, and doubles as a
group-management helper (polls, quizzes, topic controls, DM-to-group pings).

Built for [@ronrebootedbot](https://t.me/ronrebootedbot) by Adwaith Varma.

## Features

- **Conversational AI** — Groq chat backend (`ron_ai.py`), per-chat rolling
  history, tough-love-mentor persona, Telegram Markdown formatting.
- **Live streaming replies** — long answers stream token-by-token into 1:1
  chats via progressive rich drafts (Bot API 10.1 `sendRichMessage`), with
  graceful fallback to plain messages.
- **Stop-button support** — honors Telegram 10.3 `stopped_message_generation`
  to halt an in-flight stream mid-reply.
- **Native polls** — `/poll Question? | Option A | Option B`,
  `/pollm` for multi-answer, `/quiz Q? | *Correct | Wrong` for quizzes.
- **DM-to-group pings** — `[send_group: Group Name | message]` trailer posts
  your message in a group mentioning you (fuzzy group-name resolution).
- **Forum topic management** — detects forum status via `getChat` (cached),
  auto-purges stale topics when a group disables them, `[topic: ...]` and
  `[send_topic: ...]` trailers for topic actions.
- **Quick-reply buttons** — `[buttons: Label | Label]` trailer renders inline
  keyboards; tapping a button re-sends its label as the user's message.
  `[links: Label -> https://...]` for contextual links.
- **Ephemeral welcome** — greets new joiners privately, zero group spam.
- **Typing indicators**, graceful shutdown (drains in-flight replies),
  update-offset persistence, honest `429 rate limited` replies instead of
  generic error text.

## Quick start

```bash
git clone <this-repo>
cd ron-telegram-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in BOT_TOKEN and GROQ_API_KEY
python3 ron_daemon.py
```

## Configuration

All config comes from environment variables (see `.env.example`). A `.env`
file next to the scripts is loaded automatically; shell-exported variables
take precedence.

| Variable        | Required | What it is                                          |
|-----------------|----------|-----------------------------------------------------|
| `BOT_TOKEN`     | yes      | Bot token from [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY`  | yes      | Groq Cloud API key (console.groq.com)               |
| `GROQ_MODEL`    | no       | Chat model (default `openai/gpt-oss-120b`)          |
| `GROQ_BASE_URL` | no       | API base URL (default Groq OpenAI-compatible)       |
| `BOT_USERNAME`  | no       | Bot username without @ (default `ronrebootedbot`)   |
| `OWNER_ID`      | no       | Your Telegram user id (reserved, currently unused)  |
| `SYSTEM_PROMPT` | no       | Override the bot persona                            |

Runtime state (`ron_daemon.offset`, `ron_ai_history.json`, `known_groups.json`,
`known_topics.json`) lives next to the scripts and is git-ignored.

## Layout

- `ron_daemon.py` — long-polling daemon, update routing, all command/trailer handlers
- `ron_ai.py` — Groq chat backend, history, model fallback on 429s
- `config.py` — environment-variable config + `.env` loader (stdlib only)
- `stream_send.py` — progressive draft streaming for 1:1 chats
- `rich_send.py` — native rich message sender with plain-text fallback
- `ephemeral_send.py` — private ephemeral messages (welcomes)
- `delete_messages.py` — message deletion helper

## License

MIT — see [LICENSE](LICENSE).
