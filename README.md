# BanHammerBot

A Telegram group moderation bot powered by **Jev's multimodal decision API**. It evaluates
a sender's display name, username, visible avatar, current message, and recent messages.
High-confidence advertising or spam decisions can automatically ban the sender and delete
their messages.

Python 3.11+. Runs with long polling; no public server, webhook, or inbound port is required.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather) and obtain its token.
2. Create a [TypeSafe API key](https://console.typesafe.ai/) with access to Jev and available credits.
3. Add the bot to your group as an administrator with **Ban users** and **Delete messages** rights.
   Telegram administrator bots receive ordinary group messages, so disabling privacy mode is not
   necessary. See the [Telegram FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).
4. Copy the configuration and fill in the two credentials and your numeric group IDs:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   For a private supergroup message link such as `https://t.me/c/1234567890/42`, the group ID is
   `-1001234567890`. Use a comma-separated list for multiple groups. Public username links do not
   contain the numeric ID. Only configured groups are moderated.

5. Install and start with [uv](https://docs.astral.sh/uv/):

   ```sh
   uv sync --frozen
   uv run banhammerbot --check
   uv run banhammerbot
   ```

   `--check` verifies Telegram authentication, absence of a webhook, and group permissions;
   it does not call Jev or charge for a decision. The first eligible group message exercises Jev.

6. The default `DRY_RUN=true` logs `would_ban` decisions without banning or deleting. Inspect the
   results against real group traffic, customize `MODERATION_POLICY`, then set `DRY_RUN=false`
   and restart to enable enforcement. Dry run still sends evidence to Jev and incurs API usage.

## How decisions work

The bot calls TypeSafe's official `POST https://api.typesafe.ai/v1/systemone`, using three
`noul` probability questions: unsolicited advertising, disruptive spam, and explicit promotion
in the profile. JPEG/PNG bytes are included as base64 assets with UUID references and the
`TypeSafe-Danger-Zone-Images: true` header. No Telegram token-bearing download URLs are sent
to Jev. See the [API reference](https://api.typesafe.ai/redoc) and
[image documentation](https://docs.typesafe.ai/images).

| Evidence | Treatment |
| --- | --- |
| Display name and username | Included on each evaluated message |
| Avatar | Most recent profile photo visible to the bot; cached for 5 minutes |
| Text/captions | Current message, including edits |
| Links | Visible links and hidden `text_link` destinations, with Telegram's UTF-16 offsets |
| Photos and image documents | Current photo or JPEG/PNG attachment, up to 2 MiB by default |
| Recent messages | Up to 5 previously observed messages by the same user in the same group, within 10 minutes |
| Other media | Media type, caption, document name, or sticker emoji; no audio/video decoding |

The enforcement score is `max(unsolicited_ad, spam)`. Profile promotion is a supporting signal
and cannot trigger a ban by itself. The questions require evidence of a violation in the
current message and treat user text and image text as evidence, not instructions.

| Score / condition | Result |
| --- | --- |
| Below `REVIEW_THRESHOLD` (default 0.70) | `allow` |
| At least 0.70 but below `BAN_THRESHOLD` (default 0.98) | `review`, recorded for operator inspection; no action |
| At least 0.98 with dry run on | `would_ban` |
| At least 0.98 with dry run off | Permanent ban and message deletion |
| API failure, bad probability, or unknown member status | No ban |

Jev image support is currently experimental according to its image documentation. The defaults
are starting points, not measured accuracy guarantees for your group. Tune the policy and
thresholds using dry-run results. The model is pinned to `jev-1.14.0`; `JEV_MODEL` can select
another version available to your account.

## Administrator commands

Send these in a configured group, as a non-anonymous administrator:

- `/status` — mode, model, and ban threshold.
- `/help` — command summary.
- `/unban 123456789` — unban that numeric user ID. Does not eject an existing member.
  In dry run this only reports what would happen. Deleted messages cannot be restored.

Commands may be addressed as `/status@YourBotName`. Regular users cannot execute administrative
commands. Their messages are still evaluated even if they begin with a slash.

Group owners, administrators, `EXEMPT_USER_IDS`, other bots, and messages sent on behalf of a
channel or anonymous administrator are excluded. Administrator status is checked again immediately
before enforcement. Private messages, channels, and join/service events do not trigger moderation.

Telegram's [banChatMember](https://core.telegram.org/bots/api#banchatmember) revokes a banned user's
message history in supergroups. The bot also requests deletion of the triggering message for
basic groups. A deletion error does not overwrite a successful ban in the audit log.

## Configuration

All settings are documented in `.env.example`. Environment variables take precedence over `.env`.
Restart after changing configuration.

- **Required:** `TELEGRAM_BOT_TOKEN`, `TYPESAFE_API_KEY`, `TELEGRAM_CHAT_IDS`.
- **Decisions:** `JEV_MODEL`, `JEV_API_URL`, `MODERATION_POLICY`, `BAN_THRESHOLD`, `REVIEW_THRESHOLD`.
- **Enforcement:** `DRY_RUN`, `EXEMPT_USER_IDS`.
- **Bounds:** `MAX_EVENT_AGE_SECONDS`, `HISTORY_TTL_SECONDS`, `HISTORY_LIMIT`,
  `AVATAR_CACHE_SECONDS`, `MAX_IMAGE_BYTES`, `API_TIMEOUT_SECONDS`.
- **Persistence:** `DATABASE_PATH` (default `data/banhammer.sqlite3`).

Names, usernames, avatar/image bytes, message content and links, numeric IDs, and the group title
are sent to your configured Jev endpoint for classification. Avatars and recent message content
are held only in bounded process memory; expired entries are evicted on subsequent use and all
are cleared on restart. History is limited to 256 chat/user pairs and avatar caching to 32 users.
The bot cannot retrieve message history from before it started, and sends only text summaries
of earlier messages, not their image bytes. Missing or inaccessible avatars are explicitly marked.

SQLite and application logs contain IDs, decisions, model version, action, and sanitized error
metadata; they do not contain raw messages, names, or images. The audit log has no automatic
retention policy. Manage its retention and access for your deployment.

## Docker

After configuring `.env`:

```sh
docker compose up --build -d
docker compose logs -f bot
```

The image runs as an unprivileged user; a named volume persists the cursor and audit database.
Keep `DATABASE_PATH` under `/app/data` in containers. To apply changes to `.env`, recreate the
container with `docker compose up -d --force-recreate`.

Run **one worker per bot token**. Existing webhooks and conflicting pollers are reported rather
than replaced. Expected transient HTTP failures receive bounded retries. Jev rate-limit cooldowns
leave messages unmoderated until the provider's retry time. API failures are audited and that
update is acknowledged without enforcement; a later edit or message can be evaluated again.
Unexpected application errors stop the worker before acknowledging the failing update.

The cursor persists across restarts. Finished moderation records prevent replaying a completed
update. A crash between a Telegram action and saving its result may repeat an API call; remote
actions and SQLite cannot be committed atomically. Messages older than 5 minutes are skipped by
default; a recent edit is evaluated using its edit timestamp. This single-worker implementation
processes updates sequentially; a sustained backlog may age out messages.

## Inspect decisions

The local SQLite audit table can be read without exposing message content:

```sh
sqlite3 -header -column data/banhammer.sqlite3 \
  'SELECT created_at, chat_id, user_id, message_id, action, decision, detail FROM audit ORDER BY update_id DESC LIMIT 20;'
```

`review` is an audit classification, not an interactive review queue or an automatic notification.

## Development

```sh
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

Tests use mocked Telegram/Jev HTTP responses and do not need credentials or ban real users.
They cover multimodal payloads, strict decision validation, rate limits, permission checks,
dry run, enforcement, edited messages, duplicate updates, context isolation, and restart cursors.
