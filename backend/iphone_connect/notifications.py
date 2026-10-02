"""What the PC keeps of the phone's notifications, and when it shows one.

Notification texts are personal. They live in memory and in one file in the
user's runtime directory (tmpfs, gone at logout, readable only by the user)
so that the panel and the CLI can show them. They are never logged.
"""

import json
import os

from .audio import STATE_DIR

LIMIT = 60

# Without a runtime directory the state falls back to the user's state
# directory, still private to the user but on disk.
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR, "iphone-connect")
STATE_FILE = os.path.join(RUNTIME_DIR, "mirror.json")

EMPTY_MEDIA = {"available": False, "player": "", "title": "", "artist": "", "album": "", "playing": False,
               "commands": []}

PUBLIC_FIELDS = ("uid", "appId", "app", "title", "subtitle", "message", "date", "category", "important",
                 "canDismiss")


class Store:
    """The phone's current notifications, newest first."""

    def __init__(self):
        self.items = {}

    def apply(self, notification):
        """Add or replace. Returns True when this uid was not known before."""
        new = notification["uid"] not in self.items
        self.items[notification["uid"]] = {key: notification.get(key) for key in PUBLIC_FIELDS}
        return new

    def remove(self, uid):
        return self.items.pop(uid, None) is not None

    def clear(self):
        self.items = {}

    def listing(self):
        # ANCS uids grow with time, so they break ties between equal dates
        ordered = sorted(self.items.values(), key=lambda n: (n["date"] or "", n["uid"]), reverse=True)
        return ordered[:LIMIT]


def mirror_enabled(config):
    return config.get("mirror", False) is True


def should_toast(notification, config):
    """Whether a desktop notification pops up for it."""
    if config.get("mirrorToasts", True) is False:
        return False
    if notification.get("preexisting") or notification.get("silent"):
        return False
    # Calls already ring through the hands-free part of this plugin.
    if notification.get("category") == "incoming-call":
        return False
    return notification.get("appId") not in config.get("mirrorMutedApps", [])


def toast_text(notification):
    """(application name, summary, body) for the desktop notification."""
    app = notification.get("app") or "iPhone"
    title = notification.get("title") or app
    lines = [text for text in (notification.get("subtitle"), notification.get("message")) if text]
    return app, title, "\n".join(lines)


def snapshot(enabled, state="off", detail="", store=None, media=None):
    return {
        "enabled": bool(enabled),
        "state": state if enabled else "off",
        "detail": detail if enabled else "",
        "notifications": store.listing() if store and enabled else [],
        "media": dict(media) if media and enabled else dict(EMPTY_MEDIA),
    }


def write_state(data, path=STATE_FILE):
    """Atomically, and readable by the user only."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def read_state(path=STATE_FILE):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return snapshot(False)
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), list):
        return snapshot(False)
    return data


def remove_state(path=STATE_FILE):
    try:
        os.remove(path)
    except OSError:
        pass
