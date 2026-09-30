"""Background service: routes call audio and shows call notifications.

Driven entirely by D-Bus signals from org.pipewire.Telephony (no polling).
"""

import signal
import subprocess

from gi.repository import Gio, GLib

from . import audio, phone
from .events import ag_path_of
from .state import CallState, CallStateMachine, InvalidTransition, from_pipewire
from .telephony import Telephony, TelephonyError

NOTIFY = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def log(message):
    print(message, flush=True)


class Daemon:
    def __init__(self):
        self.tel = Telephony()
        self.router = audio.AudioRouter()
        self.calls = {}        # path -> {"machine", "number", "name"}
        self.addresses = {}    # ag path -> bluetooth address
        self.notification_id = 0
        self.notification_call = None

    # ---- startup --------------------------------------------------------------

    def start(self):
        try:
            for path, ifaces in self.tel.gateways():
                self.addresses[path] = ifaces["org.pipewire.Telephony.AudioGateway1"].get("Address", "")
                log(f"phone connected: {path}")
            for call in self.tel.calls():
                self._adopt(call["path"], call["state"], call["number"], call["name"])
        except TelephonyError as error:
            log(f"telephony not available yet: {error}")
        if not self._any_audio_call():
            restored = self.router.end_call()  # crash recovery
            if restored:
                log("restored audio settings left over from a previous session")
        self.tel.subscribe(self.on_event)
        self.tel.bus.signal_subscribe(NOTIFY, NOTIFY, "ActionInvoked", NOTIFY_PATH, None,
                                      Gio.DBusSignalFlags.NONE, self._on_action, None)
        self._sync_audio()

    # ---- events -----------------------------------------------------------------

    def on_event(self, event):
        kind = event["type"]
        if kind == "phone-connected":
            self.addresses[event["path"]] = event.get("address", "")
            log(f"phone connected: {event['path']}")
        elif kind == "phone-disconnected":
            self.addresses.pop(event["path"], None)
            for path in [p for p in self.calls if ag_path_of(p) == event["path"]]:
                self._remove(path)
            log(f"phone disconnected: {event['path']}")
        elif kind == "call-added":
            self._adopt(event["path"], event["state"], event.get("number", ""), event.get("name", ""))
        elif kind == "call-state":
            self._set_state(event["path"], event["state"])
        elif kind == "caller-id":
            info = self.calls.get(event["path"])
            if info:
                info["number"] = event.get("number") or info["number"]
                info["name"] = event.get("name") or info["name"]
                if info["machine"].state == CallState.INCOMING:
                    self._notify_incoming(event["path"])
        elif kind == "call-removed":
            self._remove(event["path"])
        elif kind == "audio":
            log(f"audio link: {event.get('state', '')} {event.get('codec', '')}".strip())
        self._sync_audio()

    def _adopt(self, path, state, number, name):
        machine = CallStateMachine()
        self.calls[path] = {"machine": machine, "number": number or "", "name": name or ""}
        log(f"call added {path}: {state} {phone.mask(number)}")
        self._set_state(path, state)

    def _set_state(self, path, value):
        info = self.calls.get(path)
        if not info:
            return
        try:
            target = from_pipewire(value)
        except ValueError:
            log(f"ignoring unknown call state {value!r}")
            return
        machine = info["machine"]
        try:
            changed = machine.transition(target)
        except InvalidTransition as error:
            log(f"unexpected transition {error}; following the phone")
            machine.state = target
            changed = True
        if not changed:
            return
        log(f"call {path}: {target.value}")
        if target == CallState.INCOMING:
            self._notify_incoming(path)
        elif self.notification_call == path:
            self._close_notification()

    def _remove(self, path):
        if self.notification_call == path:
            self._close_notification()
        self.calls.pop(path, None)
        log(f"call removed {path}")

    def _any_audio_call(self):
        return any(info["machine"].needs_audio for info in self.calls.values())

    def _sync_audio(self):
        if self._any_audio_call():
            path = next(p for p, i in self.calls.items() if i["machine"].needs_audio)
            address = self.addresses.get(ag_path_of(path), "")
            if not address:
                return
            state = self.router.start_call(address)
            if state.get("error"):
                # SCO nodes appear a moment after the call state; retry shortly.
                GLib.timeout_add(700, self._retry_audio)
            elif state.get("modules"):
                log(f"audio routed: {state['phone_source']} -> {state['pc_sink']}, "
                    f"{state['pc_source']} -> {state['phone_sink']}")
        else:
            if self.router.end_call():
                log("audio restored")

    def _retry_audio(self):
        if self._any_audio_call():
            state = self.router.load_state() or {}
            if not state.get("modules"):
                self._sync_audio()
        return False

    # ---- notifications ----------------------------------------------------------------

    def _notify_incoming(self, path):
        info = self.calls[path]
        title = "Incoming call"
        body = info["name"] or info["number"] or "Unknown caller"
        if info["name"] and info["number"]:
            body = f"{info['name']}\n{info['number']}"
        # Omarchy's notification popups have no buttons; a click invokes the
        # "default" action. Answer/Decline buttons live in the bar panel.
        body += "\nClick to answer · Decline in the iPhone panel"
        actions = ["default", "Answer", "answer", "Answer", "decline", "Decline"]
        hints = {"urgency": GLib.Variant("y", 2), "category": GLib.Variant("s", "call.incoming"),
                 "resident": GLib.Variant("b", True)}
        try:
            result = self.tel.bus.call_sync(
                NOTIFY, NOTIFY_PATH, NOTIFY, "Notify",
                GLib.Variant("(susssasa{sv}i)", ("iPhone", self.notification_id, "phone", title, body,
                                                 actions, hints, 0)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            self.notification_id = result.unpack()[0]
            self.notification_call = path
        except GLib.Error as error:
            log(f"notification failed: {error.message}")

    def _close_notification(self):
        if self.notification_id:
            try:
                self.tel.bus.call_sync(NOTIFY, NOTIFY_PATH, NOTIFY, "CloseNotification",
                                       GLib.Variant("(u)", (self.notification_id,)), None,
                                       Gio.DBusCallFlags.NONE, 5000, None)
            except GLib.Error:
                pass
        self.notification_id = 0
        self.notification_call = None

    def _on_action(self, _conn, _sender, _path, _iface, _member, params, _data):
        notification_id, action = params.unpack()
        if notification_id != self.notification_id or not self.notification_call:
            return
        path = self.notification_call
        try:
            if action in ("default", "answer"):
                self.tel.answer(path)
                log("answered from notification")
            elif action == "decline":
                self.tel.hangup(path)
                log("declined from notification")
        except TelephonyError as error:
            log(f"notification action failed: {error}")


def run():
    daemon = Daemon()
    daemon.start()
    loop = GLib.MainLoop()

    def stop():
        daemon.router.end_call()
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    log("iphone-connect daemon running")
    loop.run()
