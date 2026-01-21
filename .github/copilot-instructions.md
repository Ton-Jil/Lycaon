# Copilot instructions for this repository ✅

**Quick summary:** This repo implements a Discord chat bot (single process) that uses Google Gemini via the `google-genai` client. The bot behavior is configured by JSON character prompts under `character_prompts/` and conversation history is stored in a local SQLite DB (`chat_history.db`). The main program is `bot.py`.

## Run & deploy 🔧
- Local start (dev):
  - Create a virtualenv and install deps: `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
  - Provide secrets via `.env`: **`DISCORD_BOT_TOKEN`** and **`GOOGLE_API_KEY`** (optional: `TARGET_CHANNEL_IDS` comma-separated)
  - Run: `python bot.py`
- Deployment (example): see `builder/bootstrap_deploy.sh` and `builder/actual_deploy.sh` (cloud build in `builder/cloudbuild.yaml`). The `actual_deploy.sh` expects a systemd service restart (example: `sudo systemctl restart my_discord_bot.service`).

## Architecture & data flow 🧭
- `bot.py` is the single entry point. Key responsibilities:
  - Load character prompt JSONs (`character_prompts/*.json`) via `load_character_definition`
  - Initialize a model session + cached system prompt via `initialize_chat_session` and `client.caches` (cache display name pattern: `{char}-{MODEL_NAME.replace('/', '-')}-system-prompt`)
  - Send user inputs (and images) to Gemini through `shared_chat_session` and `_send_message_with_retry` (uses `tenacity` exponential backoff)
  - Persist short-term history in SQLite per-character tables named with prefix `history_` (see `get_history_table_name`) and store bot settings in `bot_settings` table (key `current_character_key`)
- Image handling: attachments are converted to `Part.from_bytes(...)` and appended to the API call (see image processing block in `on_message`).
- Response length control: if Gemini responds longer than Discord limit (2000), the bot asks Gemini to shorten and retries up to 3 times.

## Key workflows & commands (Discord-side) ⚙️
- `!setchar <key>` — switch character (loads JSON `character_prompts/<key>.json` via `initialize_chat_session`)
- `!resetchat` — clear conversation history for the active character (requires admin)
- `!resetcache` — clear model caches via `client.caches`
- `!listchars` — list available characters (reads files under `character_prompts/`)
- `!autospeak on/off` — enable/disable automatic activity messages per channel
- `!talktome` — short helper to generate a conversation starter for the invoking user

## Character prompt JSON schema (discoverable patterns) 📁
Files: `character_prompts/<key>.json` where `<key>` is used as character key. Important keys the code expects:
- `character_name_display` (string) — human-readable name
- `system_instruction_user` (string) — minimal system prompt; expressive rules should be split into `persona_rules` / `response_constraints`
- `character_metadata` (string) — extra context appended to system prompt
- `persona_rules` (string) — explicit persona rules (one/two-sentence summary of voice, pronouns, forbidden behaviors). These are automatically appended to the system prompt by `load_character_definition`.
- `response_constraints` (object) — optional structured constraints (e.g. `{"min_length": 10, "max_length": 300, "forbidden_phrases": ["私はAI"]}`); `load_character_definition` performs lightweight validation and appends a textual summary to the system prompt.
- `version` (string) — schema version, e.g. `"1.0"`
- `tags` (list of strings) — free-form labels (e.g. `"執事"`, `"丁寧"`)

- `initial_model_response` (string) — example reply used when initializing session
- `conversation_examples` (list of dicts with `role` and `parts`) — converted to initial history
- `dialogue_examples` (list of strings) — few-shot style examples appended to system prompt
- `related_characters` (list of keys) — optional; loader will embed short related info (and avoid cycles)

Notes: the code assumes character keys are alphanumeric when constructing DB table names (see `get_history_table_name`).

## Persistence & debugging tips 🐞
- DB file: `chat_history.db` (SQLite). Per-character table names: `history_<key>`.
  - Inspect with: `sqlite3 chat_history.db` and `SELECT * FROM history_<key> LIMIT 10;`
- Active character saved in DB table `bot_settings` under key `current_character_key`.
- Logs: `bot.py` prints status and warnings to stdout; when deployed as systemd service, check `sudo journalctl -u my_discord_bot.service`.
- If caches/credentials are invalid: check `GOOGLE_API_KEY` and that caches are created successfully (look for `CachedContent を作成しました` log entry).

## Error handling & model behavior specifics ⚠️
- Gemini calls are retried with `tenacity` in `_send_message_with_retry` (exponential backoff, max 5 attempts). ServerError leads to retry; other exceptions bubble up.
- Response length handling: the bot enforces Discord's 2000 char limit and requests a concise rewrite when needed (up to 3 attempts).
- If `active_cache.expire_time` is past, the session re-initializes (see check in `handle_shared_discord_message`).

## External integrations & deployment variables 🌐
- Google Gemini via `google.genai` client (requires `GOOGLE_API_KEY` env var).
- Discord via `discord.py` (requires `DISCORD_BOT_TOKEN` env var).
- `builder/cloudbuild.yaml` demonstrates a GCP-based deploy flow using `gcloud compute ssh` with substitution vars: `_ZONE`, `_INSTANCE_NAME`, `_PROJECT_DIR_ON_VM`.

## Conventions & code references to follow 🔍
- Use character JSON files as single source of model system prompts and examples (`load_character_definition`).
- Naming convention for DB table: `history_<alphanumeric key>` (enforced by `get_history_table_name`).
- Cache display name formula: `{char_key}-{MODEL_NAME.replace('/', '-')}-system-prompt` — do not change arbitrarily if maintaining cache reuse.

## Missing/optional items to watch for 📝
- No unit tests or CI are present; adding basic integration tests (prompt loader, DB helpers) is recommended but not assumed here.
- Secrets must be provided via `.env` or environment, do **not** commit credentials.

---
If any section is unclear or you want additional examples (e.g., a new sample prompt or a small test harness), tell me which part to expand and I’ll iterate. ✨
