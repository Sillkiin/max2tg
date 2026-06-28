"""Best-effort: restrict a file to its owner (it may hold tokens, the chat map,
or signed media URLs). Never raises — security hardening must not crash the
bridge."""
import logging
import os
import subprocess
from pathlib import Path

_logger = logging.getLogger(__name__)


def restrict_to_owner(path: str | Path) -> None:
    """Remove inherited ACLs and grant only the current user read/write/delete
    (Windows icacls), or chmod 600 (POSIX). Best-effort and idempotent."""
    target = str(path)
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if not user:
                return
            # Qualify as DOMAIN\USER: a bare username is ambiguous when the
            # computer name equals the username (icacls resolves it to an empty
            # "MACHINE\" account and, with /inheritance:r, locks the owner out).
            domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME")
            principal = f"{domain}\\{user}" if domain else user
            subprocess.run(
                ["icacls", target, "/inheritance:r",
                 "/grant:r", f"{principal}:(R,W,D)"],
                check=False, capture_output=True,
            )
        else:
            os.chmod(target, 0o600)
    except OSError as exc:
        _logger.debug("Could not restrict permissions on %s: %s", target, exc)
