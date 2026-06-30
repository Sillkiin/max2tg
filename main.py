"""Entry point: runs first-time setup if needed, then the MAX -> Telegram bridge."""
import asyncio
import logging
import re
import sys
from pathlib import Path

from bridge import MaxToTelegramBridge
from config import apply_dotenv, load_config
from fileperms import restrict_to_owner
from setup_wizard import run_setup
from singleton import acquire_single_instance

LOG_PATH = Path(__file__).parent / "bridge.log"
LOCK_PATH = Path(__file__).parent / "max2tg.lock"

_logger = logging.getLogger(__name__)

# Telegram bot token (bot<digits>:<base64ish>) and URL secrets (?token=/?sig=).
_BOT_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_URL_SECRET_RE = re.compile(
    r"([?&](?:token|sig|access_token|key|auth)=)[^&\s'\")]+", re.IGNORECASE)
# MAX login/auth token in a logged dict/JSON ("token": "<opaque>"): scrub the
# value. vkmax is already quieted to WARNING; this is defense-in-depth.
_MAX_TOKEN_RE = re.compile(r"""(['"]token['"]\s*:\s*['"])[^'"]{8,}(['"])""")


class _RedactSecretsFilter(logging.Filter):
    """Scrub bot tokens and URL secrets from every record before it is written.

    requests exceptions embed the full request URL — which contains the bot
    token / signed MAX URLs — so a logged exception would otherwise leak secrets
    into bridge.log (CWE-532)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _BOT_TOKEN_RE.sub("bot<redacted>", message)
        redacted = _URL_SECRET_RE.sub(r"\1<redacted>", redacted)
        redacted = _MAX_TOKEN_RE.sub(r"\1<redacted>\2", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _restrict_log_perms() -> None:
    """Lock bridge.log to the current user (it can hold message content)."""
    restrict_to_owner(LOG_PATH)


def _configure() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.addFilter(_RedactSecretsFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
    )
    # vkmax logs every packet at INFO, including auth tokens - keep it quieter
    logging.getLogger("vkmax").setLevel(logging.WARNING)
    _restrict_log_perms()


def main() -> None:
    _configure()
    # Single-instance guard: a duplicate bridge double-forwards every message.
    # An OS-level lock catches duplicates the launcher's process scan can't see
    # (e.g. a boot-task instance running in another Windows session).
    if not acquire_single_instance(LOCK_PATH):
        _logger.warning("Another max2tg instance already holds the lock; exiting.")
        print("Мост уже запущен (другой экземпляр) — этот процесс выходит.")
        return
    apply_dotenv()  # load a local .env file if present (any launch method)
    config = load_config()
    if config is None:
        # No config: the setup wizard needs an interactive console + browser.
        # On a server (no TTY) fail loudly instead of hanging on input().
        if not sys.stdin or not sys.stdin.isatty():
            print("Конфигурация не найдена. На сервере задайте переменные "
                  "окружения MAX2TG_TELEGRAM_BOT_TOKEN, MAX2TG_TELEGRAM_CHAT_ID, "
                  "MAX2TG_MAX_TOKEN, либо положите рядом готовый config.json "
                  "(см. DEPLOY.md).", file=sys.stderr)
            raise SystemExit(1)
        print("Конфигурация не найдена - запускаю мастер настройки.")
        config = run_setup()
    bridge = MaxToTelegramBridge(config)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        print("Остановлено.")


if __name__ == "__main__":
    main()
