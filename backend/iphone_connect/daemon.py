"""Background service: routes call audio and shows call notifications.

Driven entirely by D-Bus signals from org.pipewire.Telephony (no polling).
"""

import os
import signal
import subprocess

from gi.repository import Gio, GLib

from . import audio, contacts, phone
from .events import ag_path_of
from .state import CallState, CallStateMachine, InvalidTransition, from_pipewire
from .telephony import Telephony, TelephonyError

NOTIFY = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def log(message):
    print(message, flush=True)


DEFAULT_RINGTONE = "/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga"


class Ringer:
    """Local ringtone on the PC while a call is incoming.

    The phone's own in-band ringtone would need the Bluetooth voice link, and
    accepting that link early would pull the call to the PC even when it is
    answered on the iPhone. A local sound keeps "answer where you pick up".
    """

    def __init__(self):
        self.proc = None
        self.ringing = False

    def start(self):
        config = audio.load_config()
        if self.ringing or config.get("ringtone", True) is False:
            return
        self.ringing = True
        self._play(config.get("ringtoneFile") or DEFAULT_RINGTONE)
        log("ringing on the PC")

    def _play(self, path):
        if not self.ringing:
            return
        try:
            self.proc = Gio.Subprocess.new(["setpriv", "--pdeathsig", "TERM", "pw-play", "--media-role=Notification", path],
                                           Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            log(f"ringtone failed: {error.message}")
            self.ringing = False
            return

        def done(proc, result):
            try:
                proc.wait_finish(result)
            except GLib.Error:
                pass
            if self.ringing and proc is self.proc:
                GLib.timeout_add(50, lambda: (self._play(path), False)[1])  # loop like the phone

        self.proc.wait_async(None, done)

    def stop(self):
        if not self.ringing:
            return
        self.ringing = False
        if self.proc:
            self.proc.force_exit()
            self.proc = None


class Daemon:
    def __init__(self):
        self.tel = Telephony()
        self.router = audio.AudioRouter()
        self.calls = {}        # path -> {"machine", "number", "name"}
        self.addresses = {}    # ag path -> bluetooth address
        self.notification_id = 0
        self.notification_call = None
        self.ringer = Ringer()

    # ---- startup --------------------------------------------------------------

    def start(self):
        try:
            for path, ifaces in self.tel.gateways():
                self.addresses[path] = ifaces["org.pipewire.Telephony.AudioGateway1"].get("Address", "")
                log(f"phone connected: {path}")
            for call in self.tel.calls():
                self._adopt(call["path"], call["state"], call["number"], call["name"])
            if self.addresses:
                if not self.calls:
                    self._calls_stay_on_phone()
                self._sync_contacts_if_stale()
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
            if not self.calls:
                self._calls_stay_on_phone(event.get("address") or None)
            self._sync_contacts_if_stale()
            self._sync_history_later(2)
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
        self._update_ringer()
        self._sync_audio()

    def _calls_stay_on_phone(self, address=None):
        """Default: refuse the phone's voice link so iPhone-side calls stay there."""
        try:
            self.tel.set_reject_sco(True, address)
        except TelephonyError as error:
            log(f"could not set RejectSCO: {error}")

    def _cli(self, *args):
        cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "iphone-connect")
        try:
            Gio.Subprocess.new([cli, *args], Gio.SubprocessFlags.STDOUT_SILENCE)
            return True
        except GLib.Error as error:
            log(f"{' '.join(args)} could not start: {error.message}")
            return False

    def _sync_history_later(self, delay=4):
        # the phone writes the finished call into its log a moment after it ends
        GLib.timeout_add_seconds(delay, lambda: (self._cli("history", "sync"), False)[1])

    def _sync_contacts_if_stale(self, max_age=12 * 3600):
        import time
        info = contacts.cache_info()
        if info and time.time() - info["updated"] < max_age:
            return
        cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "iphone-connect")
        try:
            Gio.Subprocess.new([cli, "contacts", "sync"], Gio.SubprocessFlags.STDOUT_SILENCE)
            log("syncing contacts from the phone (PBAP) in the background")
        except GLib.Error as error:
            log(f"contact sync could not start: {error.message}")

    def _adopt(self, path, state, number, name):
        machine = CallStateMachine()
        self.calls[path] = {"machine": machine, "number": number or "",
                            "name": name or contacts.lookup(number)}
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
        self._sync_history_later()

    def _update_ringer(self):
        if any(i["machine"].state == CallState.INCOMING for i in self.calls.values()):
            self.ringer.start()
        else:
            self.ringer.stop()

    def _any_audio_call(self):
        return any(info["machine"].needs_audio for info in self.calls.values())

    def _sync_audio(self):
        if self._any_audio_call():
            path = next(p for p, i in self.calls.items() if i["machine"].needs_audio)
            address = self.addresses.get(ag_path_of(path), "")
            if self.router.load_state() is None:
                self.router.start_call(address)
                # WirePlumber links the SCO streams itself; report once they exist.
                GLib.timeout_add(1500, self._report_audio, address)
        else:
            if self.router.end_call():
                log("audio settings checked/restored")
            if self.addresses and not self.calls:
                self._calls_stay_on_phone()

    def _report_audio(self, address):
        try:
            info = self.router.describe(address)
            if info["muted"]:
                # WirePlumber restores the last mute state of the stream, so a
                # call muted at its end would start muted. Always start unmuted.
                self.router.set_mute(False, address)
                log("microphone was restored muted by WirePlumber; unmuted for the new call")
        except RuntimeError as error:
            log(f"audio check failed: {error}")
            return False
        if info["routed"] and audio.noise_suppression_enabled() and not info["noiseSuppression"]:
            try:
                self.router.enhance(address)
                log("noise suppression and echo cancellation (WebRTC) active")
            except RuntimeError as error:
                log(f"noise suppression not available, using the raw microphone: {error}")
        if info["routed"]:
            log(f"audio: phone -> {info['output']}, {info['microphone']} -> phone ({info['rate']})")
        else:
            if self._any_audio_call():
                return True  # GLib repeats this check until the streams exist
            log("audio: call ended before its audio streams appeared")
        return False

    # ---- notifications ----------------------------------------------------------------

    def _notify_incoming(self, path):
        if audio.load_config().get("notifications", True) is False:
            return  # disabled by the user: iphone-connect notifications off
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
        daemon.ringer.stop()
        daemon.router.end_call()
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    log("iphone-connect daemon running")
    loop.run()
