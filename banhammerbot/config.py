import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_POLICY = (
    "Disallow unsolicited advertising, referral promotions, scams, and disruptive spam. "
    "Allow relevant discussion, genuine recommendations, and quoted examples of spam."
)


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    typesafe_key: str = field(repr=False)
    chat_ids: frozenset[int] = frozenset()
    model: str = "jev-1.14.0"
    api_url: str = "https://api.typesafe.ai/v1/systemone"
    dry_run: bool = True
    ban_threshold: float = 0.98
    exempt_user_ids: frozenset[int] = frozenset()
    policy: str = DEFAULT_POLICY
    database_path: str = "data/banhammer.sqlite3"
    max_event_age: int = 300
    history_ttl: int = 600
    history_limit: int = 5
    avatar_cache_seconds: int = 300
    bio_cache_seconds: int = 300
    max_image_bytes: int = 2 * 1024 * 1024
    api_timeout: float = 15.0

    def __post_init__(self) -> None:
        if not self.telegram_token or not self.typesafe_key:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TYPESAFE_API_KEY in .env")
        if any(i >= 0 for i in self.chat_ids):
            raise ValueError("TELEGRAM_CHAT_IDS must be empty or contain negative group IDs")
        if any(i <= 0 for i in self.exempt_user_ids):
            raise ValueError("EXEMPT_USER_IDS must contain positive user IDs")
        if not 0 < self.ban_threshold <= 1:
            raise ValueError("BAN_THRESHOLD must satisfy 0 < BAN_THRESHOLD <= 1")
        for name in (
            "max_event_age",
            "history_ttl",
            "history_limit",
            "avatar_cache_seconds",
            "bio_cache_seconds",
            "max_image_bytes",
            "api_timeout",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.history_limit > 20 or self.max_image_bytes > 10 * 1024 * 1024:
            raise ValueError("Use at most 20 history messages and 10 MiB per image")
        if not self.policy.strip() or len(self.policy) > 2000 or not self.model.strip():
            raise ValueError("Set a model and a moderation policy of 1–2000 characters")
        url = urlsplit(self.api_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("JEV_API_URL must be an HTTPS endpoint without embedded credentials")

    @classmethod
    def from_env(cls) -> "Config":
        def ids(name: str) -> frozenset[int]:
            try:
                return frozenset(
                    int(v.strip()) for v in os.getenv(name, "").split(",") if v.strip()
                )
            except ValueError:
                raise ValueError(f"{name} must be comma-separated numeric IDs") from None

        dry_run = os.getenv("DRY_RUN", "true").lower().strip()
        if dry_run not in {"true", "false"}:
            raise ValueError("DRY_RUN must be true or false")
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            typesafe_key=os.getenv("TYPESAFE_API_KEY", "").strip(),
            chat_ids=ids("TELEGRAM_CHAT_IDS"),
            model=os.getenv("JEV_MODEL", "jev-1.14.0"),
            api_url=os.getenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone"),
            dry_run=dry_run == "true",
            ban_threshold=float(os.getenv("BAN_THRESHOLD", "0.98")),
            exempt_user_ids=ids("EXEMPT_USER_IDS"),
            policy=os.getenv("MODERATION_POLICY", DEFAULT_POLICY),
            database_path=os.getenv("DATABASE_PATH", "data/banhammer.sqlite3"),
            max_event_age=int(os.getenv("MAX_EVENT_AGE_SECONDS", "300")),
            history_ttl=int(os.getenv("HISTORY_TTL_SECONDS", "600")),
            history_limit=int(os.getenv("HISTORY_LIMIT", "5")),
            avatar_cache_seconds=int(os.getenv("AVATAR_CACHE_SECONDS", "300")),
            bio_cache_seconds=int(os.getenv("BIO_CACHE_SECONDS", "300")),
            max_image_bytes=int(os.getenv("MAX_IMAGE_BYTES", "2097152")),
            api_timeout=float(os.getenv("API_TIMEOUT_SECONDS", "15")),
        )
