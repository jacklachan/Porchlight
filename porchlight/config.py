"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

RING_API_BASE = "https://api.amazonvision.com"
RING_OAUTH_URL = "https://oauth.ring.com/oauth/token"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # Ring Partner API
    ring_api_base: str = RING_API_BASE
    ring_oauth_url: str = RING_OAUTH_URL
    ring_access_token: str | None = None
    ring_refresh_token: str | None = None
    ring_client_id: str | None = None
    ring_client_secret: str | None = None
    ring_hmac_signing_key: str | None = None
    ring_device_id: str | None = None

    # Storage
    data_dir: Path = field(default_factory=lambda: Path("data"))

    # Vision: "bedrock", "openai" (any OpenAI-compatible endpoint), "fixture" (stand-in frames only)
    # or "none" (everything goes to human review)
    vision_provider: str = "none"
    vision_base_url: str = "https://api.openai.com/v1"
    vision_api_key: str | None = None
    vision_model: str = "gpt-4o-mini"
    bedrock_model_id: str = "us.amazon.nova-lite-v1:0"
    aws_region: str = "us-east-1"
    confidence_threshold: float = 0.75

    # Assistant (the simulated Alexa+ surface): "bedrock" or "rules"
    assistant_provider: str = "rules"
    assistant_model_id: str = "us.amazon.nova-lite-v1:0"

    # Engine
    poll_seconds: int = 20
    porch_check_minutes: int = 15
    early_grace_minutes: int = 30
    late_grace_minutes: int = 60
    default_collect_within_minutes: int = 180
    timezone: str = "Asia/Kolkata"
    person_name: str = "Mom"

    # Outbound notification (optional generic webhook, POSTed a JSON alert)
    notify_webhook_url: str | None = None

    # Serving
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str | None = None
    mcp_auth_token: str | None = None
    api_token: str | None = None

    # Demo mode mounts the local Ring stand-in at /sim and enables the time controls.
    demo: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "porchlight.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def base_url(self) -> str:
        return (self.public_base_url or f"http://{self.host}:{self.port}").rstrip("/")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        demo = _flag("PORCHLIGHT_DEMO")
        host = env("PORCHLIGHT_HOST", "127.0.0.1")
        port = _int("PORCHLIGHT_PORT", _int("PORT", 8000))
        s = cls(
            ring_api_base=env("RING_API_BASE", RING_API_BASE).rstrip("/"),
            ring_oauth_url=env("RING_OAUTH_URL", RING_OAUTH_URL),
            ring_access_token=env("RING_ACCESS_TOKEN") or None,
            ring_refresh_token=env("RING_REFRESH_TOKEN") or None,
            ring_client_id=env("RING_CLIENT_ID") or None,
            ring_client_secret=env("RING_CLIENT_SECRET") or None,
            ring_hmac_signing_key=env("RING_HMAC_SIGNING_KEY") or None,
            ring_device_id=env("RING_DEVICE_ID") or None,
            data_dir=Path(env("PORCHLIGHT_DATA_DIR", "data")),
            vision_provider=env("VISION_PROVIDER", "none").lower(),
            vision_base_url=env("VISION_BASE_URL", "https://api.openai.com/v1"),
            vision_api_key=env("VISION_API_KEY") or None,
            vision_model=env("VISION_MODEL", "gpt-4o-mini"),
            bedrock_model_id=env("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0"),
            aws_region=env("AWS_REGION", env("AWS_DEFAULT_REGION", "us-east-1")),
            confidence_threshold=_float("CONFIDENCE_THRESHOLD", 0.75),
            assistant_provider=env("ASSISTANT_PROVIDER", "rules").lower(),
            assistant_model_id=env("ASSISTANT_MODEL_ID", env("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")),
            poll_seconds=_int("POLL_SECONDS", 20),
            porch_check_minutes=_int("PORCH_CHECK_MINUTES", 15),
            early_grace_minutes=_int("EARLY_GRACE_MINUTES", 30),
            late_grace_minutes=_int("LATE_GRACE_MINUTES", 60),
            default_collect_within_minutes=_int("DEFAULT_COLLECT_WITHIN_MINUTES", 180),
            timezone=env("PORCHLIGHT_TIMEZONE", "Asia/Kolkata"),
            person_name=env("PORCHLIGHT_PERSON_NAME", "Mom"),
            notify_webhook_url=env("NOTIFY_WEBHOOK_URL") or None,
            host=host,
            port=port,
            public_base_url=env("PUBLIC_BASE_URL") or None,
            mcp_auth_token=env("MCP_AUTH_TOKEN") or None,
            api_token=env("PORCHLIGHT_API_TOKEN") or None,
            demo=demo,
        )
        if demo and "RING_API_BASE" not in os.environ:
            # In demo mode with no explicit base, talk to the bundled stand-in.
            s.ring_api_base = f"http://127.0.0.1:{port}/sim"
            s.ring_access_token = s.ring_access_token or "sim-token"
            s.ring_hmac_signing_key = s.ring_hmac_signing_key or "sim-signing-key"
            if "VISION_PROVIDER" not in os.environ:
                s.vision_provider = "fixture"
        return s
