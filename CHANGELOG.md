# Changelog

## 1.5.0 — 2026-09-20

Final hardening: the bot now backs itself up and reports its own health —
the last pieces before calling it release-stable and self-sufficient.

- **Nightly encrypted snapshots** (`ron_backup.py`, new) — every night at
  03:00 IST the memory DB and cache DB are copied with SQLite's online
  backup API (consistent snapshot, safe against the live daemon's writers),
  bundled with the JSON state files into a tar.gz, and Fernet-encrypted
  with the same `RON_MEMORY_KEY` the memory DB uses (key read from the
  daemon's environment or the systemd override — never printed, logged, or
  stored; derivation imported from `ron_memory`, not duplicated). Last 7
  daily snapshots kept, older pruned; every bundle is decrypted and
  integrity-checked (`PRAGMA quick_check` + JSON parse) before the run
  counts as good. Missing key → loud log + non-zero exit, and no
  unencrypted backup is ever written (fails closed, like the memory
  store). Scheduled via `ron-backup.timer` (systemd; cron one-liner
  documented in the README for cron-based boxes)
- **`/health` command** (`ron_health.py`, new) — compact status card:
  uptime, brain state, AI chain health by position (primary / fallback-N —
  provider-neutral, backend names never appear), memory on/off + summary
  counts, cache hit-rate and tokens saved, backup freshness (⚠️ flagged
  when the nightly run is failing), watchdog restart count. Cheap local
  reads only — no AI call, works in light mode and degraded mode, follows
  the existing command routing (DMs + groups)
- Small supporting changes: `ron_ai.get_provider_cooldowns()`,
  `MemoryStore.count_summaries()` (counts rows only, never reads summary
  content), daemon stashes probe results for the card instead of probing
  live on every `/health`
- Still zero new pip dependencies; all existing features untouched

## 1.4.0 — 2026-09-20

Token savings round two: the bot now skips API calls for questions it already
answered and stops sending unbounded raw history on every turn.

- **Semantic answer cache** (`ron_cache.py`, new) — before hitting the AI
  chain, the daemon checks a per-chat cache for a semantically equivalent
  recent question. Similarity is local and dependency-free (normalized text,
  token-set overlap over content words, edit distance for short queries —
  no hosted vectors, no embeddings). Strict per-chat isolation, 24h TTL,
  200-entry LRU per chat. Commands, freshness-sensitive questions ("latest",
  "today", prices, dates), memory-write ops, and any reply built on another
  chat's consented private fact are never cached; error/degraded replies are
  never stored. Hits/misses/estimated tokens saved tracked in
  `ron_cache_stats.json`
- **Rolling history summaries** (`ron_ai.py`, `ron_memory.py`) — the JSON
  store keeps up to 48 raw turns per chat; once it passes 40, the oldest half
  is summarized by the cheapest provider model (third-person, neutral,
  continuity-focused) and folded into a summary stored in the encrypted
  per-chat memory DB (`chat_meta` table — no new plaintext store). Prompts
  now carry summary + last 15 turns instead of full raw history; any
  summarization failure silently falls back to the old behavior. `forget` /
  wipe clears the summary with everything else
- Still zero new pip dependencies; all existing features untouched

## 1.3.0 — 2026-09-20

Memory lifecycle release: memory now handles change, forgetting, and recall
like it should — negation-aware updates, complete forgetting, and relevance-ranked
recall.

- **Negation-aware contradiction replacement** (`ron_memory.py`) — a negated
  fact ("my exam is not Monday") now supersedes the old one ("my exam is
  Monday") on a single shared word, since the negation itself is the signal.
  `supersession_chain()` walks a fact's full lineage oldest → newest,
  strictly chat-scoped
- **Complete forgetting** — `forget` expands matched facts to their whole
  supersession chains and removes orphan consent / pending-approval rows for
  deleted facts. `forget_detailed()` returns the per-kind breakdown;
  `forget()` keeps its int return for compatibility
- **Richer retrieval** — `recall_ranked()` ranks with FTS5 `bm25()` relevance
  (recency breaks ties only), AND-then-OR fallback; `recall_all()` returns
  one unified facts-plus-messages list, facts weighted above messages. The
  daemon's memory block uses it directly
- Still deterministic, zero LLM calls, per-chat isolation untouched

## 1.2.0 — 2026-09-20

Privacy-first memory release: the bot now remembers per chat, encrypted at
rest, with consent-gated disclosure — plus a lean light mode and Bot API
10.3 rich buttons.

- **Encrypted per-chat memory** (`ron_memory.py`, new) — SQLite with
  Fernet encryption (key from `RON_MEMORY_KEY`; fails closed without it).
  Strict isolation by `chat_id`: DM, group, and Business memories never
  merge. Explicit facts only (`remember that…` / `forget that…`), local
  FTS5 recall, raw messages expire after 30 days, `forget` wipes per chat,
  nothing sensitive in logs
- **Disclosure consent** (`ron_privacy.py`, new) — when a private fact
  becomes relevant in a group, the bot says nothing publicly and DMs the
  person for approval instead. Group-text "yes" never counts; consent is
  scoped to one group + user + fact. The approval DM now renders as a
  rich message with semantic buttons (✅ Yes = green, ❌ Keep private =
  red), falling back to a classic keyboard if rich send fails
- **Accidental-oversharing guard** — if the owner asks something personal
  in a group, the bot asks for approval privately before answering
  publicly. DMs bypass the guard
- **Light mode** (`light_mode` in `ron_ai.json`) — cheap-model routing,
  no draft streaming, single rich send per reply, one daily notice per
  user. Built for heavy lifting, running lean on demand
- **Bot API 10.3 rich message buttons** — `InputRichBlockButtons` blocks
  where the bot owns the full message; styled inline keyboards stay where
  the body is AI-generated markdown
- New dependency: `cryptography` (memory encryption); everything else
  remains stdlib. `RON_MEMORY_KEY` documented in `.env.example`

## 1.1.0 — 2026-09-20

Self-sufficiency release: the multi-provider brain, token economy, and
self-healing supervision that landed on the live bot after 1.0.1.

- **Multi-provider AI brain** (`ron_ai.py`) — failover chain Groq
  (`openai/gpt-oss-120b` → `openai/gpt-oss-20b`) → Gemini
  (`gemini-3.8-flash` → `gemini-flash-lite-latest`) → OpenRouter
  (`nex-agi/nex-n2.5-pro:free` → `nvidia/nemotron-3.5-lightning:free`),
  tunable via `ron_ai.json`. Per-provider 429 cooldowns, 401/403 skips the
  rest of that provider, model-not-found skips just that model. Providers
  without a key are skipped silently — the bot runs fine on Groq alone.
  Errors stay provider-neutral; only the owner (if `OWNER_ID` is set) gets
  a private heads-up about a dead key
- **Token economy (Phase 2)** — a failed stream after full chain exhaustion
  goes straight to the honest error instead of re-running the whole chain;
  greetings and small talk try each provider's smallest model first with a
  short prompt; pure acknowledgements (`ok`, `lol`, `thanks`, `👍`, `gn`)
  get canned replies with zero API calls (still logged to history)
- **Watchdog supervisor** (`ron_watchdog.py`, new) — 60-second checks for
  dead/hung daemons (stale heartbeat), duplicate processes, and invalid
  `ron_ai.json`; exponential restart backoff (60s → 5min → 15min → 1h);
  broken configs are quarantined and the last-known-good copy restored.
  Ships with `ron-watchdog.service.example` for systemd
- **Degraded mode** — after 5 systemic AI failures in 10 minutes the bot
  stops burning API calls, tells users honestly their message is queued,
  persists the queue to `degraded_queue.jsonl`, and replays it on recovery
  without duplicates; canned acknowledgements keep working throughout
- **Business / Secretary Mode** — handles `business_connection` and
  `business_message` updates: replies go to the customer through the
  owner's business account as one rich message (no draft streaming there)
- Release packaging: `GEMINI_API_KEY` / `OPENROUTER_API_KEY` added to
  `config.py` and `.env.example`; `ron_ai.json` ships the default model
  chain; all runtime state files are git-ignored

## 1.0.1 — 2026-09-20

Runtime sync: three batches that landed on the live bot after 1.0.0.

- **Ephemeral group welcomes wired up** — the joiner-only welcome popup
  (`ephemeral_message_parameters`) was defined but never called; it now fires
  for each new human member (bots skipped), with a dedicated message for the
  Muse Playground group and a generic fallback elsewhere
- **12 stability fixes** — stall watchdog no longer tripped by long reasoning
  traces (plus a 600s absolute stream cap), history/state file write races
  fixed (locks + atomic tmp+rename writes), orphaned stream bubble on failure
  fixed, `STOP_EVENTS` clobbering fixed, per-update offset saves, silent
  message loss on bad keyboards fixed (`callback_data` truncated to 64 bytes),
  UTF-16-aware group mention detection, `/cmd@otherbot` ignored, expired
  Playground watch hook removed
- **Native draft streaming** — 1:1 streaming migrated from the
  `sendMessage`/`editMessageText` loop to Telegram's native `sendMessageDraft`
  (Bot API 9.3+): instant "Thinking…" placeholder, throttled draft growth,
  native stop button via `can_stop`/`keep_on_stop` with graceful fallback,
  `stopped_message_generation` handling; the final reply still goes out via
  `sendRichMessage` with full formatting
- Added `TELEGRAM_DOCS_NOTES.md` — distilled notes from the official Telegram
  Bot docs that drove the streaming migration

## 1.0.0 — 2026-09-20

Initial release. Snapshot of the bot running as `@ronrebootedbot`.

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
