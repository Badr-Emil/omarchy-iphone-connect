"""Holds the one data channel to the phone and mirrors its notifications.

The iPhone accepts a single ATT channel per computer and refuses a second one
until its Bluetooth is toggled, so the background service keeps this channel
for as long as the phone is connected and everything else (panel, CLI) talks
to the service.
"""

import errno
import os
import re
import socket

from gi.repository import GLib

from . import att, notifications
from .audio import load_config
from .companion import Session

RETRY_SECONDS = (5, 15, 30, 60, 120, 300)
REFUSED_RETRY_SECONDS = 300
TICK_SECONDS = 3
PERMISSION_RETRY_TICKS = 10      # ask again every 30 s while the phone says "not allowed"
PUBLISH_DELAY_MS = 150

NOTIFY = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def open_channel(address):
    """Start connecting to the phone's ATT channel; the socket is non-blocking."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    sock.setblocking(False)
    code = sock.connect_ex((address, att.PSM))
    if code not in (0, errno.EINPROGRESS):
        sock.close()
        raise OSError(code, os.strerror(code))
    return sock


class Toaster:
    """Desktop notifications for mirrored ones, one per phone notification."""

    def __init__(self, bus):
        self.bus = bus
        self.ids = {}   # phone uid -> desktop notification id

    def show(self, notification):
        app, summary, body = notifications.toast_text(notification)
        uid = notification["uid"]
        hints = {"urgency": GLib.Variant("y", 1), "category": GLib.Variant("s", "im.received"),
                 "desktop-entry": GLib.Variant("s", "iphone-connect")}
        try:
            result = self.bus.call_sync(
                NOTIFY, NOTIFY_PATH, NOTIFY, "Notify",
                GLib.Variant("(susssasa{sv}i)", (app, self.ids.get(uid, 0), "phone", summary, body, [], hints, -1)),
                GLib.VariantType("(u)"), 0, 5000, None)
            self.ids[uid] = result.unpack()[0]
        except GLib.Error:
            pass   # no notification daemon: the panel still lists it

    def close(self, uid):
        desktop_id = self.ids.pop(uid, 0)
        if not desktop_id:
            return
        try:
            self.bus.call_sync(NOTIFY, NOTIFY_PATH, NOTIFY, "CloseNotification", GLib.Variant("(u)", (desktop_id,)),
                               None, 0, 5000, None)
        except GLib.Error:
            pass

    def forget(self):
        self.ids = {}


class Mirror:
    def __init__(self, toaster, log=print, opener=open_channel, config_loader=load_config,
                 state_path=notifications.STATE_FILE):
        self.toaster = toaster
        self.log = log
        self.opener = opener
        self.config_loader = config_loader
        self.state_path = state_path
        self.config = config_loader()
        self.store = notifications.Store()
        self.address = None
        self.sock = None
        self.session = None
        self.state = "off"
        self.detail = ""
        self._watch = 0
        self._ticker = 0
        self._retry_timer = 0
        self._publish_timer = 0
        self._attempt = 0
        self._ticks = 0

    # ---- what the service tells the mirror ------------------------------------------

    def phone_connected(self, address):
        self.address = address if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address or "") else None
        self.refresh()

    def phone_disconnected(self):
        self.address = None
        self._close()
        self._set_state("waiting", "the iPhone is not connected")

    def refresh(self):
        """Apply the configuration: called at start and whenever it changes."""
        self.config = self.config_loader()
        if not self.enabled:
            self._close()
            self._cancel_retry()
            self.toaster.forget()
            self._set_state("off")
        elif self.sock is None and not self._retry_timer:
            if self.address:
                self._connect()
            else:
                self._set_state("waiting", "the iPhone is not connected")
        self._publish()

    def stop(self):
        self._close()
        self._cancel_retry()
        if self._publish_timer:
            GLib.source_remove(self._publish_timer)
            self._publish_timer = 0
        notifications.remove_state(self.state_path)

    @property
    def enabled(self):
        return notifications.mirror_enabled(self.config)

    # ---- actions ----------------------------------------------------------------------

    def dismiss(self, uid):
        return bool(self.session and self.session.dismiss(uid))

    def media(self, command):
        return bool(self.session and self.session.media_command(command))

    # ---- the channel ------------------------------------------------------------------

    def _connect(self):
        self._cancel_retry()
        try:
            self.sock = self.opener(self.address)
        except OSError as error:
            return self._lost(error.strerror or str(error), refused=error.errno == errno.ECONNREFUSED)
        self._set_state("connecting")
        self._watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_OUT | GLib.IO_ERR | GLib.IO_HUP, self._connected)

    def _connected(self, _fd, _condition):
        self._watch = 0
        code = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if code:
            self._lost(os.strerror(code), refused=code == errno.ECONNREFUSED)
            return False
        self.session = Session(self._send, on_state=self._session_state, on_notification=self._notification,
                               on_removed=self._removed, on_media=lambda _media: self._publish())
        self._watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._readable)
        self._ticks = 0
        self._ticker = GLib.timeout_add_seconds(TICK_SECONDS, self._tick)
        self._set_state("discovering")
        self.session.start()
        return False

    def _readable(self, _fd, condition):
        while self.sock is not None:
            try:
                packet = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError as error:
                self._watch = 0
                self._lost(error.strerror or str(error))
                return False
            if not packet:
                self._watch = 0
                self._lost("the iPhone closed the data channel")
                return False
            self.session.feed(packet)
        if self.sock is None:     # the session failed while handling a packet
            self._watch = 0
            return False
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            self._watch = 0
            self._lost("the iPhone closed the data channel")
            return False
        return True

    def _send(self, packet):
        try:
            self.sock.send(packet)
        except (OSError, AttributeError):
            pass   # the watch on the socket reports the broken channel

    def _tick(self):
        if self.session is None:
            self._ticker = 0
            return False
        self._ticks += 1
        if self.session.state == "needs-permission" and self._ticks % PERMISSION_RETRY_TICKS == 0:
            self.session.retry()
        if not self.session.tick():
            self._ticker = 0
            self._lost(self.session.detail)
            return False
        return True

    def _close(self):
        for name in ("_watch", "_ticker"):
            if getattr(self, name):
                GLib.source_remove(getattr(self, name))
                setattr(self, name, 0)
        if self.session:
            self.session.close()
            self.session = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.store.clear()

    def _cancel_retry(self):
        if self._retry_timer:
            GLib.source_remove(self._retry_timer)
            self._retry_timer = 0

    def _lost(self, reason, refused=False):
        self._close()
        if not self.enabled or not self.address:
            return
        if refused:
            delay = REFUSED_RETRY_SECONDS
            reason = "the iPhone refused the data channel; switch Bluetooth off and on in the iPhone's settings"
        else:
            delay = RETRY_SECONDS[min(self._attempt, len(RETRY_SECONDS) - 1)]
            self._attempt += 1
        self.log(f"notification channel lost ({reason}); next try in {delay} s")
        self._set_state("waiting", reason)

        def again():
            self._retry_timer = 0
            if self.enabled and self.address and self.sock is None:
                self._connect()
            return False

        self._cancel_retry()
        self._retry_timer = GLib.timeout_add_seconds(delay, again)

    # ---- what the session reports -------------------------------------------------------

    def _session_state(self, state, detail):
        if state == "failed":
            return   # _tick or _readable turn this into a retry
        if state == "ready":
            self._attempt = 0
            self.log("mirroring the iPhone's notifications")
        elif state in ("needs-permission", "unsupported"):
            self.log(f"notifications not available: {detail}")
        self._set_state(state, detail)

    def _notification(self, notification):
        self.store.apply(notification)
        if notifications.should_toast(notification, self.config):
            self.toaster.show(notification)
        self._publish()

    def _removed(self, uid):
        if self.store.remove(uid):
            self._publish()
        self.toaster.close(uid)

    # ---- state for the panel and the CLI ------------------------------------------------

    def _set_state(self, state, detail=""):
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self._publish()

    def snapshot(self):
        media = self.session.media if self.session else None
        return notifications.snapshot(self.enabled, self.state, self.detail, self.store, media)

    def _publish(self):
        if self._publish_timer:
            return

        def write():
            self._publish_timer = 0
            try:
                notifications.write_state(self.snapshot(), self.state_path)
            except OSError as error:
                self.log(f"could not write the mirror state: {error}")
            return False

        self._publish_timer = GLib.timeout_add(PUBLISH_DELAY_MS, write)
