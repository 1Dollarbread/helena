"""Web Push notifications for the browser HUD.

Lets `helena-web` notify you when a turn finishes even if the tab isn't
focused or the laptop lid is down with the browser backgrounded — without
running through any third-party service beyond what Web Push itself
requires (the browser vendor's own push endpoint, e.g. Google's for Chrome —
there's no way around that for a real background push; the message content
and the VAPID identity that signs it stay between this machine and the
browser).

Needs the `push` extra (`pip install -e ".[push]"`, i.e. `pywebpush` +
`cryptography`). Everything here degrades to a quiet no-op if that isn't
installed, so `helena-web` still works without it — you just won't get
notifications while the tab is backgrounded.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

USER_DIR = Path(os.path.expanduser("~")) / ".helena"
VAPID_PATH = USER_DIR / "vapid.json"
SUBSCRIPTIONS_PATH = USER_DIR / "push_subscriptions.json"
VAPID_CLAIMS_SUB = "mailto:helena@localhost"  # required by the push protocol; never actually emailed


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def push_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    except ImportError:
        return False
    return True


def _generate_vapid_keys() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    numbers = private_key.public_key().public_numbers()
    # Browsers want the "applicationServerKey" as a raw uncompressed EC
    # point (0x04 + 32-byte X + 32-byte Y), base64url — not the PEM form.
    raw_point = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return {"private_key_pem": private_pem, "public_key": _b64url(raw_point)}


def get_vapid_keys() -> dict[str, str] | None:
    """Generated once per machine and reused from then on — the public key
    is what the browser pins a subscription to, so rotating it would
    silently orphan every subscription made before the rotation."""
    if not push_available():
        return None
    if VAPID_PATH.is_file():
        try:
            return json.loads(VAPID_PATH.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    keys = _generate_vapid_keys()
    USER_DIR.mkdir(parents=True, exist_ok=True)
    VAPID_PATH.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    return keys


def _load_subscriptions() -> list[dict[str, Any]]:
    if not SUBSCRIPTIONS_PATH.is_file():
        return []
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_subscriptions(subs: list[dict[str, Any]]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTIONS_PATH.write_text(json.dumps(subs, indent=2), encoding="utf-8")


def add_subscription(subscription_info: dict[str, Any]) -> None:
    subs = _load_subscriptions()
    if not any(s.get("endpoint") == subscription_info.get("endpoint") for s in subs):
        subs.append(subscription_info)
        _save_subscriptions(subs)


def remove_subscription(endpoint: str) -> None:
    subs = [s for s in _load_subscriptions() if s.get("endpoint") != endpoint]
    _save_subscriptions(subs)


def notify_all(title: str, body: str, tag: str = "helena-turn") -> None:
    """Best-effort and synchronous (pywebpush shells out to `requests`) —
    call this via `asyncio.to_thread` from async code. A dead subscription
    (410/404 from the push service — the browser un-subscribed, or the
    profile was removed) is dropped so it isn't retried forever; anything
    else is just logged, since a failed notification should never take down
    the turn that triggered it."""
    if not push_available() or not _load_subscriptions():
        return
    from pywebpush import WebPushException, webpush

    keys = get_vapid_keys()
    if not keys:
        return
    payload = json.dumps({"title": title, "body": body, "tag": tag})
    for sub in _load_subscriptions():
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=keys["private_key_pem"],
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                remove_subscription(sub.get("endpoint", ""))
            else:
                print(f"[push] delivery failed: {exc}")
