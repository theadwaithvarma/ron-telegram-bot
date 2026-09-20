# Telegram Bot Platform — docs notes (2026-09-20)

Source: https://core.telegram.org/bots and https://core.telegram.org/bots/features
(full read-through; Bot API 10.3, Aug 2026).

## Native streaming (the big one)
- `sendMessageDraft` (Bot API 9.3, open to all bots since 9.5) is the
  purpose-built streaming method. Our old sendMessage+editMessageText loop is
  the legacy anti-pattern (chunky edits, "edited" label, edit lag).
- Params: `chat_id` (required, **private chats only**), `draft_id` (required,
  non-zero; updates reusing the same id are animated), `text` (0–4096 chars;
  **empty text = "Thinking…" placeholder**), `parse_mode`/`entities`,
  `message_thread_id`, `can_stop` (10.3), `keep_on_stop` (10.3).
- Returns `True`. Use JSON for the call so booleans serialize correctly.
- Draft is an **ephemeral ~30s preview** — it is NOT a message. You MUST
  finalize with `sendMessage` / `sendRichMessage` (we finalize with
  `sendRichMessage`). One `draft_id` per reply operation; finalize exactly once.
- `can_stop=True` → client shows a native Stop button; the bot receives an
  `stopped_message_generation` update (`MessageGenerationStopped` on `Update`)
  → cancel generation there. `keep_on_stop=True` keeps the partial draft
  visible after stop (still expires; send it as a message to preserve it).
- `sendRichMessageDraft` exists for streaming rich content; we stream plain
  text during generation and format at the end, so plain `sendMessageDraft`
  + final `sendRichMessage` is the right combo.
- `<tg-thinking>` tag / `InputRichBlockThinking` = visible placeholder in rich
  drafts (presentation only, not a reasoning transcript).
- Streaming is documented for **private chats** — our DM-only streaming
  matches; groups keep typing-action + single send.

## Callbacks
- **Every** callback query should be answered with `answerCallbackQuery`
  (BotFather sends status alerts for "too few answers to callback queries",
  "too few replies to incoming private messages", etc.).

## Ephemeral messages
- `ephemeral_message_parameters: {receiver_user_id, callback_query_id,
  replace_callback_query_message}` on send methods AND `sendRichMessage`.
- Ephemeral + rich can be combined (personalized interactive flows).
- `editEphemeralMessageText` supports `rich_message`; reply/delete of
  ephemeral messages supported.
- `can_send_welcome_messages` admin right exists (native welcome messages —
  needs admin; our ephemeral welcome works without admin, keep it).

## Privacy mode
- Group bots see only relevant messages, but **all service messages**
  (incl. `new_chat_members`) arrive regardless — our welcome hook is safe.

## Limits / reliability
- Flood limits: use retry policies, never hardcode limit values.
- Test bots: create a second bot via @BotFather for testing (needs Adwaith).
- Language: `language_code` in updates; fall back to last-seen or English.

## Rich messages
- "Designed for highly structured responses: reports, AI-streamed answers…"
  Confirms `sendRichMessage` as our final-send. Rich Markdown (GFM-flavored)
  + Rich HTML both supported.
