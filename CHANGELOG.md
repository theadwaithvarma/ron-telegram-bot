## 1.19.1 — formatting mistake audit + fixes (2026-09-21)

Found by probing the live model with 10 formatting-heavy prompts and by
mining the daemon log:

- **Validator was silently killing pull quotes and math.** v1.18.1 taught
  the converter to emit `pullquote` / `mathematical_expression` blocks,
  but the pre-send validator's allowlist still rejected them — 8+ live
  replies fell back to Markdown with literal `>>>` / `$$` text showing.
  Both block types are now allowlisted with field checks (4 new tests).
- **Model weakness: spoilers inside table cells.** Asked to put a spoiler
  in a cell, the model wrote a malformed `||Blazing fast|` that shattered
  the row (the `||` pipes collide with column separators). Prompt now
  forbids pipes inside table cells (repo default + live override).
- **`>>>` pull quotes were swallowed by preceding `>` quote blocks** —
  the quote collector ate them as continuations. Fixed; 1 new test.
- **Unclosed `<details>` leaked raw tags as paragraph text; nested
  `<details>` leaked inner tags.** The collector now depth-counts and
  auto-closes at end of input — always a real details block, never raw
  tags. 2 new tests (one updated).
- Stale log noise investigated: a `username` NameError hit 6 streams at a
  single boot and never recurred — transient deploy mix, not a code bug.

38 + 139 validator/block tests green; full suite 560 (3 pre-existing
`cryptography` import errors unchanged).

## 1.19.1 — formatting mistake audit + fixes (2026-09-21)

Found by probing the live model with 10 formatting-heavy prompts and by
mining the daemon log:

- **Validator was silently killing pull quotes and math.** v1.18.1 taught
  the converter to emit `pullquote` / `mathematical_expression` blocks,
  but the pre-send validator's allowlist still rejected them — 8+ live
  replies fell back to Markdown with literal `>>>` / `$$` text showing.
  Both block types are now allowlisted with field checks (4 new tests).
- **Model weakness: spoilers inside table cells.** Asked to put a spoiler
  in a cell, the model wrote a malformed `||Blazing fast|` that shattered
  the row (the `||` pipes collide with column separators). Prompt now
  forbids pipes inside table cells (repo default + live override).
- **`>>>` pull quotes were swallowed by preceding `>` quote blocks** —
  the quote collector ate them as continuations. Fixed; 1 new test.
- **Unclosed `<details>` leaked raw tags as paragraph text; nested
  `<details>` leaked inner tags.** The collector now depth-counts and
  auto-closes at end of input — always a real details block, never raw
  tags. 2 new tests (one updated).
- Stale log noise investigated: a `username` NameError hit 6 streams at a
  single boot and never recurred — transient deploy mix, not a code bug.

38 + 139 validator/block tests green; full suite 560 (3 pre-existing
`cryptography` import errors unchanged).

## 1.19.0 — owner rulebook: teach the bot from Telegram (2026-09-21)

- **Teach Ron directly from your DM.** As the owner, say "remember: ...",
  "from now on, ...", "new rule: ..." or "note: ..." in your DM and it
  becomes a standing rule in `owner_rules.json` — injected into every AI
  reply's system prompt, so it shapes the bot in every chat it speaks in.
  Only your user id can teach; trigger phrases are deliberately narrow so
  normal chat never becomes a rule, and "remember this ..." still means
  per-chat memory as before.
- **`/rules`** lists the rulebook, **`/forgetrule <n>`** drops one — both
  owner-only and ephemeral like the other power commands. Rules are
  behavior config, separate from per-chat isolated memory: nothing leaks
  between users' chats.
- 15 new tests (`tests/test_owner_rules.py`); full suite 558 tests green
  (3 pre-existing `cryptography` import errors unchanged).

## 1.18.1 — pull quotes + math blocks (2026-09-21)

- **Two more native block types.** `>>>` lines now render as real pull
  quotes (`pullquote`, verified shape via live harness) and `$$expr$$`
  single-line or fenced `$$` blocks render as real math expressions
  (`mathematical_expression`). Both route through the `sendRichMessage`
  blocks form via `needs_blocks_form`. Block shapes were proven against the
  live Bot API with a 21-type send-and-delete sweep — all 21 passed.
- Prompt teaching updated (repo `DEFAULT_CONFIG` + live `ron_ai.json`):
  the model now knows `>>>` and `$$` syntax.

## 1.18.0 — native rich formatting v2 (2026-09-21)

- **Native Bot API 10.1-10.3 rich blocks, end to end.** The bot now renders
  markdown constructs that have no Markdown representation as real native
  Telegram elements instead of literal text: compact tables (first row as
  header cells, `is_compact`), checklists (`- [ ]` / `- [x]` become real
  checkboxes), true `##` headings, tap-to-expand `<details><summary>`
  sections (plus the "Tap to expand:" shorthand, collapsed by default),
  auto-collapsing long quotes (`expandable_blockquote`, 400+ chars), and
  language-tagged fenced code blocks (syntax highlighting).
- `send_rich()` now parses replies and routes any text needing these
  constructs through the `sendRichMessage` **blocks** form (with a local
  pre-flight via `ron_richvalidate`); anything else keeps the existing
  Markdown path. Any failure falls back to Markdown, then plain text —
  the reply always lands. Streaming drafts stay plain text, so native
  streaming, can_stop/stop handling, and the stop button flow are
  untouched.
- In-body button rows are documented as the official `buttons` block
  shape (1-8 per row, 64-byte callback cap, left/center/right align).
- Prompt guidance added (live `ron_ai.json` + repo default): the model
  is taught when to emit tables, checklists, headings, code languages,
  and the one allowed HTML exception (`<details><summary>`).
- **Fixed** two converter bugs found by tests: `<details>` open+close on
  one line was dropped to a paragraph, and checklist lines were swallowed
  by the plain bullet-list collector.
- Tests: new `tests/test_rich_blocks.py` (30 cases), validator updated
  for the new documented shapes (details, expandable_blockquote,
  is_header, checkbox flags, table caption/border/stripe fields),
  heading test updated to expect real heading blocks.

## 1.17.3 — honest instant /ping (2026-09-21)

- **Fixed**: /ping measured one live getMe per call, which often caught a
  cold connection pool and reported an inflated number (e.g. ~860ms vs
  the true ~200ms warm-pool latency).
- Every successful non-poll api() call now feeds a rolling exponential
  moving average of real Telegram API round-trip latency (long-poll
  getUpdates excluded — its wait time is not latency). /ping reports the
  average instantly with zero extra API calls; falls back to one live
  getMe only when there is no recent data (fresh boot).
- The heartbeat loop warms the pool with one cheap getMe every 90s, so
  the pool never goes cold and /ping (and the next real send) always
  sees warm-pool latency.
- Tests: 4 new cases in tests/test_ping.py (EMA hit needs no API call,
  stale EMA falls back live, api() feeds EMA on success, getUpdates
  excluded).

## 1.17.1 — username NameError hotfix (2026-09-21)

- **Fixed**: v1.17.0 referenced a bare `username` inside `ai_reply()`,
  but it was only defined in `handle_update()`'s scope. Every AI reply
  died with `NameError: name 'username' is not defined`; the failures
  looked systemic, degraded mode queued messages, and the bot went
  unresponsive. `ai_reply()` now takes `username=""` and all call sites
  (DM, group, business, button callbacks) pass the real handle through.
- **Regression tests**: `tests/test_username_plumbing.py` — live
  `ai_reply` calls (stream + non-stream paths) verify the username is
  forwarded with no NameError, plus a closure-aware static scope check
  that every `username` load in ron_daemon.py resolves.

# Changelog

## 1.17.0 — 2026-09-21

Public-bot identity fixes (no features removed):

- **No more profiling**: the system prompt told the bot to "tailor labels
  to this person's apparent interests" — that instruction is gone. The bot
  now treats every user as a stranger: no replies, suggestions, or buttons
  shaped around presumed interests, profession, or personality.
- **Buttons are drill-downs only**: a button must dig deeper into something
  the reply already said — never a new topic, never a personalized
  suggestion. No natural drill-down means no buttons.
- **Real @username in context**: the requester's actual Telegram handle is
  now passed into the prompt (`_who_line`), so the bot mentions @TheAdwaith
  instead of inventing "@Adwaith". Works per-requester —
  correct for any user, not just the owner. Invalid handles are dropped.
- **Config override lesson**: `ron_ai.DIR` is hardcoded to the live dir, so
  `~/workspace/telegram-bot/ron_ai.json` overrides the repo's
  DEFAULT_CONFIG at runtime. Prompt-text edits must land in the live json;
  the repo default is kept in sync as fallback.

## 1.16.1 — 2026-09-21

- **`/ping` now reports real latency**: times one live `getMe` round-trip
  over the keep-alive pool and replies `🏓 Pong — {ms}ms` (bot→Telegram
  latency through the egress proxy). API failure is reported honestly
  instead of a fake number.

## 1.16.0 — 2026-09-21

Overnight stability + speed pass: no features removed, nothing user-facing
changed — every AI reply just starts sooner and the hot paths do less work.

- **AI brain on the keep-alive pool** (`ron_ai._request` now streams
  chat-completions through `http_pool` instead of a fresh
  `urllib.request.urlopen` per call): one proxy CONNECT + TLS handshake
  (~0.4s on this host) removed from every AI request before model
  generation begins. Startup provider probes use the pool too. Retry,
  provider-cooldown (429/401/5xx), failover order, and the API semaphore
  (still held for the full stream lifecycle) are unchanged.
- **Stream lifecycle hygiene** (`http_pool.stream()` + `_PooledStream`):
  clean `[DONE]` / fully-read responses drain (bounded 3s) and recycle the
  connection; stop-button, error, or stall paths `abort()` and discard it
  so a dead stream is never reused.
- **History rollover off the critical path** (`_maybe_rollover_async`):
  folding old turns into the rolling summary used to cost a full extra
  summarization round-trip *before* the reply's own AI call; now it runs
  on a background thread (one per chat, guarded) and the reply goes out
  immediately with the prior summary. A lost worker is harmless — the
  next turn past the threshold retries.
- **Group/topic context computed once** (`ai_reply`): the private
  streaming path used to build `group_context()` (disk reads) and then
  throw it away while `stream_ai_reply` rebuilt it; now it's computed
  only on the non-streaming path that actually uses it.
- **`note_group()` write debounce**: `known_groups.json` was rewritten on
  every group message; unchanged entries now skip the disk write for
  60s (the `seen` timestamp is write-only metadata — nothing reads it).
- **`ephemeral_send.py` on the pool** (saves a handshake per ephemeral
  send) and a **watchdog fd-leak fix** (`start_daemon` closed over the
  log file handle once per restart).
- 16 new tests (pool streaming recycle/abort, pooled AI transport incl.
  429/401/5xx classification, async rollover, group-write debounce);
  484 run, only the 3 pre-existing `cryptography` loader errors (system
  Python lacks the package).

## 1.15.1 — 2026-09-21

Contextual quick-reply buttons: the model kept emitting generic boilerplate
("Ask", "Summary", "Fix code") on replies they had nothing to do with.

- **Daemon-side filter** (`_button_fits_context`, wired into
  `parse_trailers` so both the classic keyboard and the in-bubble rich
  buttons get it): boilerplate labels are always dropped; code-action
  labels ("Fix this", "Debug it") survive only when the reply shows code;
  summary labels survive only on replies long enough to summarize (>=400
  chars); every other label must share a real topic word with the reply.
  Tiny labels ("Yes"/"No") pass through. Dropped buttons log loudly
  (`[ron_daemon] trailer: off-topic button dropped: …`). `[links:]`
  trailers (real URLs) are untouched.
- **Prompt retrained**: buttons default to NONE, every label must contain
  a topic word from the reply, casual one-liners get no buttons at all.
- 11 new tests; 468 run, only the 3 pre-existing `cryptography` loader
  errors (system Python lacks the package).

## 1.15.0 — 2026-09-21

Rich-text fix loop: the whole live-fire matrix is green against the real
Bot API, and every finding is now enforced locally before anything is sent.

- **Correct block shapes** (verified live 2026-09-21, enforced by the new
  `ron_richvalidate` pre-flight check on every `sendRichMessage`):
  blockquote carries nested `blocks`, list items carry
  `{"blocks": [{"type": "paragraph", ...}]}`, tables carry `cells`
  (not `rows`). Wrong shapes fail LOUD in the logs
  (`[ron_daemon] RICH VALIDATION FAILED: …`) and the user gets a plain-text
  fallback — the invalid rich request is never sent.
- **Validator stays stricter than the server on purpose.** Telegram
  silently accepts three ambiguous payloads; we reject them locally so a
  sloppy AI draft can never depend on server leniency: markdown + blocks
  together, a button with two action fields, a button label given as a
  RichText dict.
- **Keyboard failures heal correctly.** `BUTTON_DATA_INVALID` and other
  button errors are now their own heal class: the bad keyboard is dropped
  and the text retried without `reply_markup` (stripping formatting can't
  fix a bad keyboard). Every `RICH_MESSAGE_*` error classifies as healable.
- Converter fixes: asymmetric inline nesting (`*a **b** c*` nests
  properly), `c++`/`c#` fence languages, empty/unclosed fences never emit
  an empty `pre` (the server 400s those), tables keep `||spoiler||` cells
  intact and parse inline formatting per cell.
- Trailer hardening: lone `*`/`_` stripped from button labels, non-http
  `[links:]` targets keep the label as visible text (and log loudly)
  instead of vanishing, `||`/`==` stripped in the plain-text fallback.
- `/imagine` photo captions go out as plain text (no `parse_mode`) — an
  unbalanced marker in a prompt no longer 400s the whole upload; a
  caption rejection retries once without the caption.
- **`/del` and `/edit` removed** (scope cut). They fall through to the
  conversational AI; all handlers, resolvers, tests, and docs are gone.
- Deliberate limitations: block types outside the verified set are
  rejected by the validator (fail loud, don't guess); `#`/`@` stay plain
  text on the blocks path (no hashtag/mention entities there).

## 1.14.0 — 2026-09-21

Self-healing send pipeline: the bot now detects its OWN formatting
errors and fixes them automatically — no user action needed for these.

- All outbound text sends (`send()`, `send_markup()`, rich `sendRichMessage`
  via `send_blocks()`/`send_rich()`) and `editMessageText` go through
  `send_healed()`, which classifies Telegram API failures and retries:
  - Parse/entity errors ("can't parse entities",
    `RICH_MESSAGE_CONTENT_REQUIRED`) → strip ALL formatting, retry as
    plain text (inline keyboard kept).
  - Length errors ("message is too long", "caption is too long") →
    truncate to Telegram limits (4096 text / 1024 caption, marked "…").
- Retry chain: original → fix matching the error → the other fix (a
  message can be malformed AND too long) → give up with the existing
  provider-neutral error. Max 3 attempts, never infinite; unknown errors
  fail fast without guessing.
- Bad rich blocks can't be fixed in place, so `sendRichMessage` failures
  are retried as plain `sendMessage` (text extracted from the blocks).
- Silent on success — the user just sees the fixed message. Every
  auto-recovery is logged (`[ron_daemon] send healed: …`) so it shows
  up in `/logs`.
## 1.12.0 — 2026-09-21

Owner power commands (all owner-only, all ephemeral — the command and the
bot's reply auto-delete after 30 seconds, silently):

- `/stats` — today's traffic: inbound/outbound counts, active chats,
  per-provider AI call counts, cache hit rate, uptime.
- `/ban <user_id>` / `/unban <user_id>` — silence a user everywhere
  (messages, button taps, Business chats); ban list persists on disk.
- `/broadcast <text>` — two-step: stages the message and shows a
  preview, then `/broadcast yes` within 60 seconds sends to every known
  chat (`/broadcast no` cancels). Expired requests are rejected.
- `/say <chat_id> <text>` — send text as the bot into any chat.
- `/restart` — clean restart: drops a flag the watchdog honors, so the
  relaunch doesn't burn the crash counter or enter backoff.
- `/quiet <minutes>` — maintenance mode with auto-resume (1–1440 min);
  `/shutdown` stays manual, `/start` wakes early.
- `/logs [n]` — tail of the daemon log as a monospace code block.
- `/users` — full user roster with first/last seen.
- `/forgetuser <user_id>` — wipes the user's turns everywhere plus their
  whole private-DM chat (facts, summaries, bot replies); other chats'
  facts are chat-scoped and untouched. MemoryStore.wipe_user() corrected
  to include the DM chat's facts and metadata.
- `/model [groq|gemini|openrouter]` — show the provider chain or pin one
  first (persisted to ron_ai.json).
- `/active` retrofitted with the same ephemeral behavior.
- Outbound accounting: /stats now counts successful sends on every path
  (plain send(), send_markup(), and rich sendRichMessage), and only
  counts successful sends.
- ron_ai: AI_CALL_HOOK fires before each provider attempt (drives the
  per-provider counters); user turns tagged with user_id, stripped
  before the prompt reaches providers.
- Tests: new tests/test_powercmds.py (51 tests: gating, bans +
  enforcement, broadcast confirm/expiry, quiet expiry, forgetuser
  scoping, ephemeral timers, model pinning, restart flag, outbound
  accounting); MemoryStore.wipe_user scoping test in test_memory.py.

## 1.11.0 — 2026-09-21

New owner-only `/active` command: shows who's around right now and in
the last 10 minutes (owner-approved).

- Activity ledger: every handled update (message, button tap, business
  chat) records user_id -> {name, username, chat, timestamp} in an
  in-memory dict — one dict write per update, no API calls, no disk.
  Pruned to the 10-minute window on read.
- Reply has two sections: "Now" (users with a reply currently
  generating, tracked via the in-flight reply threads, plus anyone
  active in the last 60s) and "Last 10 min" (everyone else in the
  window, newest first, with relative times). Empty case:
  "No active users in the last 10 min."
- Non-owners get the standard owner-only refusal ("Only Adwaith can
  use that."), same convention as /shutdown.
- Tests: new tests/test_active.py (17 tests: recording, hooks,
  window filtering, in-flight tracking, owner gating, empty case).

## 1.10.0 — 2026-09-21

`/factcheck` and `/think` removed (owner-approved).

- Both commands, their routing, handlers, quota scopes, system-prompt
  mentions, and owner quota-ping paths are gone. `/factcheck` /
  `/think` now fall through to the conversational AI like any other
  unknown command.
- Deleted `ron_search` (web search backend, only used by `/factcheck`)
  and `ron_cmdquota` (per-command quotas, only used by these two
  scopes). `/imagine` and image-reading quotas are untouched — they
  live in `ron_imagen` / `ron_vision` with their own owner pings.
- `ron_ai.single_shot` stays as a general one-off completion utility.
- Tests: `test_spotcmds.py` removed; `TestSingleShot` coverage kept in
  new `tests/test_single_shot.py`. 211/214 green (3 pre-existing
  failures: `cryptography` not installed in this VM's python —
  `test_backup`, `test_memory`, `test_summary` — identical before
  this change).

## 1.9.2 — 2026-09-21

Owner quota visibility.

- Whenever any user hits a quota cap (`/factcheck`, `/think`,
  `/imagine`, image reads), the owner now gets a DM with the usage:
  `🚦 Quota: <name> (<id>) hit the /factcheck cap — 10/10 per 6h.`
- Throttled to one ping per user per command per window, so a user
  hammering a capped command doesn't spam the owner. The owner is
  exempt and never triggers a ping; a failed ping never breaks the
  user flow.
- New `usage()` getters on `ron_cmdquota`, `ron_imagen`, `ron_vision`
  reporting the current in-window count.
- 11 new tests (ping throttle, owner exemption, usage counts,
  handler-level owner ping). 335/335 green.

## 1.9.1 — 2026-09-21

`/factcheck` now works with zero API keys.

- New `ron_search` module: web search backend with a free-first
  preference order — Tavily (1,000 free searches/month, no card) when
  its key is connected in the vault, otherwise a keyless DuckDuckGo
  lite scrape. Stdlib only, no new dependencies.
- `/factcheck` backend chain, best first: Perplexity `sonar-pro` (when
  its key is connected) -> Tavily -> keyless DuckDuckGo. The verdict is
  rendered by Groq gpt-oss-120b (Gemini flash fallback) from the search
  evidence, with a strict True/False/Misleading/Unverifiable format.
- Source links are appended by the daemon from the real search results
  — the model never invents URLs.
- The old "not connected" refusal is gone: the command always works
  now, degrading gracefully only if the web itself is unreachable.
- DDG parsing is throttle-aware (one short backoff retry).
- 12 new tests (DDG parsing, uddg unwrap, source formatting, backend
  preference, verdict paths incl. Gemini fallback). 324/324 green.

## 1.9.0 — 2026-09-21

Two new brain-switch commands: `/factcheck` and `/think`.

- `/factcheck <claim>` — pinned to Perplexity's `sonar-pro` for
  web-grounded verdicts (True / False / Misleading / Unverifiable +
  citations). 10 uses per rolling 6h per user, owner exempt — the API
  is paid, so the quota is checked before any call and spent only on
  success. Needs a Perplexity API key in the vault; without it the
  command says so politely instead of burning anything.
- `/think <question>` — pinned to Gemini `gemini-3.8-flash` with a
  deep-reasoning prompt, for questions that deserve real thought.
  15 uses per rolling 6h per user, owner exempt.
- New `ron_ai.single_shot(provider, model, messages)`: one-off
  completion pinned to a single provider — no history, no memory hooks,
  no cheap routing. Reuses the battle-tested `_request()` retry/backoff
  internals with a one-provider plan.
- New `ron_cmdquota` module: generic sliding-window per-command quotas
  (scope, N, window), same shape as the imagine/vision limiters.
- Both commands run in worker threads off the poll loop, with a "…"
  ack that's deleted before the answer lands (mirrors /imagine).
  History keeps the Q&A so context continues.
- The bot's capability blurb (system prompt) now mentions both commands.
- Perplexity stays OUT of the default chat failover chain — normal chat
  never touches the paid API.
- 23 new tests (quota, prefix parsing, provider pinning, no-key and
  quota-block paths, success/failure worker paths). 312/312 green.

## 1.8.5 — 2026-09-21

More speed: the typing indicator + "Thinking…" draft were sent
serially *before* the AI call started — ~0.4s of pure Telegram
round-trips blocking the thing that actually takes time.

- New `typing_kickoff()` helper: fires `sendChatAction("typing")` in a
  background thread while the AI request starts immediately. Applied to
  all three reply paths — DM draft streaming (typing + empty draft now
  overlap the AI call), group/business text replies, and group vision
  replies. The kickoff is joined (10s timeout) after generation so the
  final send never races the draft setup.
- Test doubles updated: the `SyncThread` stand-ins in the vision/imagen
  tests now accept `args=`/`kwargs=` and provide `join()`/`is_alive()`,
  matching the real `threading.Thread` signature.
- 3 new kickoff tests (AI starts while typing is in flight, draft-before-
  final ordering). 289/289 green.

## 1.8.4 — 2026-09-21

Speed fix: every Telegram API call was opening a brand-new HTTPS
connection, and through this host's proxy that's ~0.7s of CONNECT + TLS
handshake per call. A single reply makes 3-6 of them (typing indicator,
empty draft, draft updates, final send) — all before the AI even starts.

- New `http_pool` module: a small thread-safe pool of persistent
  keep-alive connections per host, with proxy CONNECT tunneling that
  mirrors urllib's proxy handling (env vars + no_proxy).
- `ron_daemon.api()`, `rich_send.api_json()`, and the imagine
  `sendPhoto` upload now go through the pool: ~0.7s -> ~0.2s per call,
  roughly 1.5-2s shaved off every reply, zero behavior change.
- Stale (server-closed) pooled connections are discarded and retried
  once on a fresh connection — but only when no response arrived, so a
  send is never duplicated.
- 7 new pool tests (reuse, stale retry, no-retry-after-response,
  proxy auth, no_proxy bypass, thread safety). 286/286 green.

## 1.8.3 — 2026-09-21

`/imagine` reliability fix: some generations arrived half-done (top half
image, bottom half grey) and were uploaded as-is. Two compounding causes
found: the provider's CDN was killing Python-urllib connections
mid-stream (curl downloads completed 10/10, urllib 0/10 through the same
egress path), and the downloader trusted the first short `read()` without
ever verifying the download was complete.

- Download backend is now curl first (immune to the connection kills),
  with the stdlib urllib path as fallback where curl isn't installed.
- Integrity gate on every download: declared Content-Length must match
  the bytes received, and the JPEG end-of-image marker must be present.
  Cut-off downloads are treated as transient — retried with backoff,
  never uploaded.
- Reads loop until the declared length or EOF (a single socket `read()`
  can silently return one 8KB chunk).
- Model chain: `flux` first (full-quality diffusion — now reachable via
  curl), `turbo` as automatic fallback when flux is saturated.


## 1.8.2 — 2026-09-20

Spam-guard hotfix. A user hammering a quick-reply button re-sent the
button label as a message per tap; callback queries bypassed the
maintenance-mode gate, each tap burned an AI call, and the degraded
queue piled up duplicates that replayed as a group flood.

- `handle_callback` now honors maintenance mode for non-owners
  (silently ignored, like text messages).
- Button-mash throttle: one tap per user+button per 30s; rapid repeat
  taps are dropped before they reach the AI.
- Degraded-queue dedupe: an identical (chat, user, text) entry already
  queued is not appended again.


## 1.8.1 — 2026-09-20

Hotfix for `/imagine`: the image provider's default backend started
429ing fresh generations (its upstream queue saturates under load),
which surfaced as "the image service hiccuped".

- Switched generation to the provider's `turbo` model — fresh 1024x1024
  images generate reliably in a few seconds.
- Added retry with backoff (3 attempts) for transient flakes: HTTP
  429/5xx, dropped connections, timeouts. Definitive failures (empty
  prompt, non-image response) still fail fast with no retry.


## 1.8.0 — 2026-09-20

The bot can now draw. `/imagine a red fox in the snow` generates an
image and sends it back as a photo, in DMs and groups.

- **Image generation** (`/imagine`, `ron_imagen.py`) — prompt goes to a
  free keyless image API (1024x1024 JPEG), uploaded via `sendPhoto`
  multipart. Nothing runs on the bot itself: generation adds zero weight
  and never slows regular chat — it only fires on an explicit command,
  in a worker thread. A "Generating…" placeholder is deleted once the
  photo lands; failures get an honest provider-neutral message and never
  name the backend.
- **Fair quota** — 10 successful generations per rolling 6 hours for
  everyone; the owner is exempt. The limit is checked before any network
  call, and quota is spent only after a successful generation.
- **Privacy-first** — image bytes never touch history, memory, logs, or
  durable storage; history keeps `[generated image]` + prompt. `/health`
  reports images generated in the last 24h (merged into the images line
  to keep the card at 8 lines).


## 1.7.0 — 2026-09-20

The bot can now see. Send it a photo in DM (or mention it with a photo in
a group) and it describes what it sees, powered by Gemini vision.

- **Image reading** — photos arrive via a worker-thread pipeline
  (`ron_vision.py`): largest Telegram size → RGB → max 768px → JPEG q80 →
  base64 to `gemini-flash-lite-latest` (fallback `gemini-3.8-flash`).
  Quota: 5 successful reads per rolling 6 hours for everyone; the owner
  is exempt. Group photos are only read when the bot is mentioned in the
  caption or the photo replies to the bot. Image bytes never touch
  history, memory, logs or disk — history keeps `[photo]` + caption, and
  vision runs with no history or memory attached. `/health` reports image
  reads from the last 24h. Photos sent as files get a "resend as photo"
  notice.
- **Rich in-bubble buttons fixed** — button rows were being sent with an
  invalid block type and silently fell back to a keyboard *under* the
  message bubble. Messages carrying buttons now go out as valid
  `InputRichBlockButtons` blocks (`ron_richblocks.py`: Markdown → blocks,
  one buttons block per row), with the classic keyboard and plain-text
  fallbacks preserved.
- **Button label sanitizer** — the AI kept leaking Markdown and raw
  `Label -> https://...` link syntax into button text. Labels are now
  stripped of all formatting server-side, and misplaced link syntax is
  rescued into a proper URL button instead of showing raw junk.
- **Watchdog hardening** — two restart-loop bugs fixed: (1) `start_daemon()`
  now unlinks the stale `ron_heartbeat.json` before launching, so a fresh
  daemon is never misread as "hung" and SIGTERMed before its first beat;
  (2) `ron-watchdog.service` uses `KillMode=process`, so restarting the
  watchdog no longer kills the detached daemon (and no longer counts as a
  crash → hour-long backoff).
- 224/224 tests green, including 10 new vision tests and 5 new button-label
  sanitizer tests.

## 1.6.0 — 2026-09-20

Owner-only maintenance mode: Adwaith can now quiet the bot for everyone
but himself without touching the server.

- **`/shutdown`** (owner only) — flips the bot into maintenance mode:
  every DM, group message and business-chat message from anyone else is
  silently ignored (no replies, no AI calls burned). The owner's own
  chats keep working normally.
- **`/start`** — during maintenance, the owner wakes the bot back up for
  everyone; anyone else just gets a short "taking a break" notice.
  Outside maintenance `/start` still flows to the conversational AI
  (no scripted greeting), as before.
- The flag persists in `ron_maintenance.json`, so it survives daemon
  restarts; 10 new tests cover the flag, both commands and the update
  gate (159/159 green).

## 1.5.1 — 2026-09-20

Reboot-survival fix, learned the hard way: a host reboot wiped the
ephemeral `/etc/systemd/system`, taking the watchdog unit and the memory
key's systemd override with it. The bot was down ~15 minutes; the old
memory key was unrecoverable, so the encrypted memory DB was archived and
re-keyed (recent chat histories survive in the plaintext JSON history).

- **Persistent secrets** — `RON_MEMORY_KEY` and proxy env now live in a
  root-only `~/.config/ron/env` file (mode 0600, on persistent storage),
  loaded via `EnvironmentFile=` — no more secrets in `/etc`.
- **Canonical systemd units** — new `systemd/` directory holds the real
  `ron-watchdog.service`, `ron-backup.service`, `ron-backup.timer`, plus
  `install.sh`, which reinstalls everything into `/etc` after a host
  reboot (one command: `systemd/install.sh`).
- `ron_backup.py` now also reads the key from the persistent env file
  (env var → persistent file → legacy override), so backups work even
  when no systemd override exists.

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
