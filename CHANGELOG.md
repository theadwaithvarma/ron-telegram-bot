# Changelog

## 1.0.0 — 2026-09-20

Initial public release. Snapshot of the bot running as `@ronrebootedbot`.

- Conversational AI backend on Groq (`openai/gpt-oss-120b`) with per-chat
  rolling history and model fallback on 429 rate limits
- Live token streaming into 1:1 chats via progressive rich drafts
  (Bot API 10.1), with plain-text fallback
- Telegram 10.3 stop-button support (`stopped_message_generation`)
- Native polls: `/poll`, `/pollm` (multi-answer), `/quiz`
- DM-to-group pings via `[send_group: Group | message]` trailers with fuzzy
  group-name resolution and requester mentions
- Forum topic management: `getChat` forum detection (cached), automatic stale
  topic purge, `[topic: ...]` / `[send_topic: ...]` trailers
- Quick-reply inline buttons (`[buttons: ...]`) and contextual links
  (`[links: ...]`) trailers; button taps re-send as user messages
- Ephemeral new-member welcomes (visible only to the joiner)
- Typing indicators, graceful shutdown draining in-flight replies,
  persisted update offset, per-chat history
- Config fully via environment variables (`.env.example`); no hardcoded
  secrets or ids
