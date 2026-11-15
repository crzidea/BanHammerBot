# BanHammerBot

A Telegram group moderation bot powered by **Jev's multimodal decision API**. It evaluates
a sender's display name, username, available bio, visible avatar, current message, and recent messages.
High-confidence advertising or spam decisions can automatically ban the sender and delete
their messages.

Python 3.11+. Runs with long polling; no public server, webhook, or inbound port is required.
By default it moderates every group where it has **Ban users** and **Delete messages** admin rights.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather) and obtain its token.
2. Create a [TypeSafe API key](https://console.typesafe.ai/) with access to Jev and available credits.
3. Add the bot to your group as an administrator with **Ban users** and **Delete messages** rights.
   Telegram administrator bots receive ordinary group messages, so disabling privacy mode is not
   necessary. See the [Telegram FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).
4. Copy the configuration and fill in the two credentials:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   Leave `TELEGRAM_CHAT_IDS=` empty to moderate every group where the bot has the required
   admin rights. New groups work automatically as messages arrive; no restart or group list is
   needed. To limit moderation, optionally enter a comma-separated list of numeric group IDs.

5. Install and start with [uv](https://docs.astral.sh/uv/):

   ```sh
   uv sync --frozen
   uv run banhammerbot --check
   uv run banhammerbot
   ```

   `--check` verifies Telegram authentication and absence of a webhook. When an explicit group
   list is configured, it also checks those groups' permissions. In all-groups mode, permissions
   are checked as messages arrive. This command does not call Jev or charge for a decision.
   The first eligible group message exercises Jev.

6. The default `DRY_RUN=true` logs `would_ban` decisions without banning or deleting. Use `/banlog`
   to inspect the results against real group traffic and customize `MODERATION_POLICY`. Open the
   bot's private chat, use `/select`, then `/dryrun off` to enable enforcement in that group. Dry run still sends
   evidence to Jev and incurs API usage. The environment setting is the default for groups without
   a saved override; changing a group in DM takes effect immediately and survives restarts.

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
| Bio | Included when Telegram's `getChat` exposes it; results cached for 5 minutes (`BIO_CACHE_SECONDS`), including unavailable results; lookup limited to 3 seconds |
| Avatar | Most recent profile photo visible to the bot; cached for 5 minutes |
| Text/captions | Current message, including edits |
| Links | Visible links and hidden `text_link` destinations, with Telegram's UTF-16 offsets |
| Photos and image documents | Current photo or JPEG/PNG attachment, up to 2 MiB by default |
| Recent messages | Up to 5 previously observed messages by the same user in the same group, within 10 minutes |
| Other media | Media type, caption, document name, or sticker emoji; no audio/video decoding |

The enforcement score is `max(unsolicited_ad, spam)`. Profile promotion is a supporting signal
and cannot trigger a ban by itself. The questions require evidence of a violation in the
current message and treat user text, bios, and image text as evidence, not instructions.
Missing, empty, or inaccessible bios are omitted without preventing message analysis.

| Score / condition | Result |
| --- | --- |
| Below `BAN_THRESHOLD` (default 0.98) | `allow`; no action |
| At or above `BAN_THRESHOLD` with dry run on | `would_ban` |
| At or above `BAN_THRESHOLD` with dry run off | Permanent ban and message deletion |
| API failure, bad probability, or unknown member status | No ban |

Jev image support is currently experimental according to its image documentation. The defaults
are starting points, not measured accuracy guarantees for your group. Tune the policy and
ban threshold using dry-run results. The model is pinned to `jev-1.14.0`; `JEV_MODEL` can select
another version available to your account.

## Private management menu

Open the bot's DM and send `/start` or `/menu`. Telegram's command menu and inline buttons provide
group selection, recent ban logs, dry-run controls, and **4 / 12 / 24 hour** new-member mute presets.
Setting names are separate from their controls, with a check marking the active choice.
Dry run has On / Off buttons. Join mute has an Off button, with duration choices below;
selecting a duration enables the mute. Buttons update the current menu in place, with group
switching and refresh together at the bottom.

| Command in DM | Action |
| --- | --- |
| `/groups` or `/select` | Choose a group from the menu |
| `/select -1001234567890` | Select your actual numeric group ID directly |
| `/menu` or `/status` | Show the selected group and its current settings |
| `/banlog` | Browse recent bans and dry-run ban decisions for the selected group |
| `/dryrun on` | Simulate moderation for the selected group |
| `/dryrun off` | Enable enforcement for the selected group |
| `/joinmute 4h`, `/joinmute 12h`, `/joinmute 24h` | Set the new-member mute duration |
| `/joinmute on` | Re-enable the previously selected mute duration |
| `/joinmute off` | Disable automatic mutes for future joins |
| `/unban 123456789` | Unban that user in the selected group |
| `/help` | Show command help |

Your selection is personal; settings are shared by that group's administrators. Every selection,
setting change, and button click verifies current group ownership or admin **Ban users** permission,
and verifies the bot has **Ban users** and **Delete messages**. A button from an older menu always
targets the group displayed in that menu. Other groups' settings are unaffected.

The group directory is populated from group messages, bot/member membership events, and existing
audit metadata. If a group is absent, send `/status` in the group or use `/select <group ID>` in DM.
Only groups you can currently manage are displayed. Previously unseen groups cannot be enumerated
from Telegram; they must produce an update or be selected by ID first.

In groups, `/menu`, `/status`, and `/help` provide a link to the private management panel.
`/banlog` provides a link directly to that group's private ban log; audit entries are not posted
in the group. The log link verifies permissions before showing entries.
`/unban <user ID>` also works for authorized admins directly in a group. Commands can be addressed
as `/status@YourBotName`. Regular users cannot run admin commands; their group messages are still
evaluated if they begin with a slash. Edited private messages do not execute commands.

## New-member mute

Join mute is **off by default**. In DM, select a supergroup and choose **4 hours**, **12 hours**,
or **24 hours**. Custom durations via `/joinmute` support 60 seconds to 30 days; an integer without
a unit means seconds. The same group's dry-run setting controls whether a mute is actually applied.
Dry run records `would_mute`; `/dryrun off` enables the configured mute for future joins.

The bot listens for member transitions and join service messages, checks the new member's current
role, and deduplicates join actions in SQLite. Bots, administrators, exempt users, departed users,
and users already restricted by someone else are skipped. Mute duration runs from the join time;
old events and those with fewer than 45 seconds remaining are skipped. A failed restriction is
recorded without retrying an aging deadline.

Telegram expires timed restrictions itself, so unmuting does not depend on the bot staying online.
Turning `/joinmute off` affects future joins; it does not remove existing timed restrictions.
Timed restrictions require a **supergroup**, as specified by
[Telegram's restrictChatMember API](https://core.telegram.org/bots/api#restrictchatmember).

Group owners, administrators, `EXEMPT_USER_IDS`, other bots, and messages sent on behalf of a
channel or anonymous administrator are excluded. The bot checks its own admin rights before
analyzing a message and again before enforcement; losing either required permission stops action.
Sender administrator status is also checked again before enforcement. Private management messages,
channels, and service events are not sent to Jev. Joins use the optional timed-mute policy separately.

Telegram's [banChatMember](https://core.telegram.org/bots/api#banchatmember) revokes a banned user's
message history in supergroups. The bot also requests deletion of the triggering message for
basic groups. A deletion error does not overwrite a successful ban in the audit log.

## Configuration

Environment settings are documented in `.env.example`. Environment variables take precedence over
`.env`; restart after changing them. Saved group dry-run overrides take precedence over `DRY_RUN`.
Private-menu changes require no restart.

- **Required:** `TELEGRAM_BOT_TOKEN`, `TYPESAFE_API_KEY`.
- **Scope:** `TELEGRAM_CHAT_IDS` is optional. Empty or unset means every group with the required
  bot admin rights; a nonempty list restricts moderation to those numeric group IDs.
- **Decisions:** `JEV_MODEL`, `JEV_API_URL`, `MODERATION_POLICY`, `BAN_THRESHOLD`.
- **Enforcement:** `DRY_RUN`, `EXEMPT_USER_IDS`.
- **Bounds:** `MAX_EVENT_AGE_SECONDS`, `HISTORY_TTL_SECONDS`, `HISTORY_LIMIT`,
  `AVATAR_CACHE_SECONDS`, `BIO_CACHE_SECONDS`, `MAX_IMAGE_BYTES`, `API_TIMEOUT_SECONDS`.
- **Persistence:** `DATABASE_PATH` (default `data/banhammer.sqlite3`).

Names, usernames, available bios, avatar/image bytes, message content and links, numeric IDs, and
the group title are sent to your configured Jev endpoint for classification. Bios, avatars, and
recent message content are held only in bounded process memory; expired entries are evicted on
subsequent use and all are cleared on restart. History is limited to 256 chat/user pairs and
avatar caching to 32 users.
Bio caching is limited to 256 users and 1,024 characters per bio.
The bot cannot retrieve message history from before it started, and sends only text summaries
of earlier messages, not their image bytes. Missing or inaccessible avatars are explicitly marked.

SQLite stores the group directory (IDs, titles, types), per-group settings, each manager's selected
group, join actions, polling cursor, and moderation audit. Audit records and application logs contain
IDs, decisions, model version, action, and sanitized error metadata; no raw messages, user names, bios,
or images are persisted. There is no automatic retention policy. Manage retention and access for
your deployment. Existing databases gain the management tables automatically on startup.

## Docker

After configuring `.env`:

```sh
docker compose up --build -d
docker compose logs -f banhammerbot
```

Compose builds and runs the image `banhammerbot:latest`.
The image runs as an unprivileged user; a named volume persists the cursor and audit database.
Keep `DATABASE_PATH` under `/app/data` in containers. To apply changes to `.env`, recreate the
container with `docker compose up -d --force-recreate`.

Stop a local foreground bot with **Ctrl+C**, or the container with `docker compose stop`.
SIGINT and SIGTERM cancel active startup, polling, and moderation requests immediately, then
close the HTTP client and database. An interrupted update is left unacknowledged for restart.
`docker compose logs -f banhammerbot` only follows logs; Ctrl+C there stops the log viewer, not the bot.

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

## Troubleshooting startup

Run `uv run banhammerbot --check`. Errors identify the Telegram method, numeric chat ID when
applicable, and a sanitized Telegram description. Tokens and URLs are redacted.

For `telegram.getChat ... Bad Request: chat not found`, check `TELEGRAM_CHAT_IDS` in `.env`.
Clear the old example ID (`TELEGRAM_CHAT_IDS=`) to enable all-groups mode, or replace it with
your actual group IDs if you want a restricted list. A migrated group error reports its new ID.
For missing admin permissions, grant both **Ban users** and **Delete messages**.

## Inspect decisions

Group owners and admins with **Ban users** permission can open **Recent ban log** in the private
management menu, or select a group and send `/banlog`. Sending `/banlog` in a group provides a
button to open its log in private chat. The bot must still have its required moderation permissions.

The log shows confirmed bans and **Dry run: would ban (no action)** decisions together, regardless
of the group's current dry-run setting. Entries show the recorded time in UTC, a clickable user name,
the triggering message ID/link, and the violation score when available. **Older** and **Newer**
browse five entries at a time; **Refresh** returns to the latest entries. Permissions are rechecked
on every request and page change. Existing audit records are available after upgrading; a query
index is added automatically at startup.

Names are looked up from Telegram when each page opens, so they reflect current display names,
not necessarily the name at the time of the decision. Names are not saved in the audit database;
unavailable names fall back to the numeric user ID. Lookups run concurrently with a three-second
timeout and are shared by entries for the same user on a page.
User links are Telegram mentions generated from the saved ID and depend on Telegram's privacy
settings. Message links are provided for supergroups and may no longer work after deletion.
The log contains only this bot's recorded decisions; it does not retrieve deleted message content
or other bots' actions. Allowed messages, failed or unfinished bans, and join mutes are not listed.

The local SQLite audit table can be read without exposing message content:

```sh
sqlite3 -header -column data/banhammer.sqlite3 \
  'SELECT created_at, chat_id, user_id, message_id, action, decision, detail FROM audit ORDER BY update_id DESC LIMIT 20;'
```

Scores below the ban threshold are recorded as `allow`. Historical `review` entries remain in
existing databases; the obsolete `REVIEW_THRESHOLD` environment variable is ignored.
Automatic join mutes are recorded separately in the `join_actions` table.

## Development

```sh
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

Tests use mocked Telegram/Jev HTTP responses and do not need credentials or ban real users.
They cover multimodal payloads, strict decision validation, rate limits, permission checks,
dry run, enforcement, edited messages, duplicate updates, context isolation, restart cursors,
private-menu authorization, per-group settings, timed join mutes, and graceful signal shutdown.
