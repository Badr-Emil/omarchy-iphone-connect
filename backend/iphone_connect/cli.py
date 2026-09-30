"""iphone-connect command line interface."""

import argparse
import json
import os
import signal
import subprocess
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import audio, bluez, contacts, phone  # noqa: E402
from .telephony import Telephony, TelephonyError  # noqa: E402

LAST_NUMBER_FILE = os.path.join(audio.STATE_DIR, "last-number")
ANSWERABLE = ("incoming", "waiting")
ENDABLE = ("dialing", "alerting", "active", "held", "incoming", "waiting")


class UserError(Exception):
    pass


def log(message):
    print(message, file=sys.stderr, flush=True)


# ---- status ---------------------------------------------------------------------

def collect_status(mask_numbers=False):
    status = {"bluetooth": None, "phone": None, "hfp": False, "calls": [], "call": None,
              "transport": None, "audio": None, "volume": None, "errors": []}
    try:
        sysbus = bluez.system_bus()
        path, props = bluez.adapter(sysbus)
        status["bluetooth"] = {"adapter": path.rsplit("/", 1)[-1], "powered": bool(props.get("Powered")),
                               "address": props.get("Address", "")}
        phones = [d for d in bluez.devices(sysbus, phones_only=True) if d["paired"]]
        if phones:
            status["phone"] = phones[0]
    except bluez.BluetoothError as error:
        status["errors"].append(str(error))

    try:
        tel = Telephony()
        address = status["phone"]["address"] if status["phone"] else None
        ag_path, ifaces = tel.gateway(address)
        if ag_path:
            status["hfp"] = True
            ag = ifaces["org.pipewire.Telephony.AudioGateway1"]
            status["volume"] = {"speaker": ag.get("SpeakerVolume"), "microphone": ag.get("MicrophoneVolume")}
            status["calls"] = tel.calls(ag_path)
            status["transport"] = tel.transport(ag_path)
    except TelephonyError as error:
        status["errors"].append(str(error))

    index = contacts.load_index()
    for c in status["calls"]:
        if not c["name"]:
            c["name"] = contacts.lookup(c["number"], index)
    live = [c for c in status["calls"] if c["state"] != "disconnected"]
    order = {"incoming": 0, "waiting": 1, "active": 2, "alerting": 3, "dialing": 4, "held": 5}
    live.sort(key=lambda c: order.get(c["state"], 9))
    status["call"] = live[0] if live else None

    try:
        address = status["phone"]["address"] if status["phone"] else None
        status["audio"] = audio.AudioRouter().describe(address)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        status["errors"].append(f"audio: {error}")

    call = status["call"]
    status["missedUnseen"] = contacts.unseen_missed(contacts.load_history())
    status["capabilities"] = {
        "canDial": status["hfp"],
        "canAnswer": bool(call and call["state"] in ANSWERABLE),
        "canReject": bool(call and call["state"] in ANSWERABLE),
        "canHangup": bool(call and call["state"] in ENDABLE),
        "canSendTones": bool(call and call["state"] == "active"),
        "canMute": bool(status["audio"] and status["audio"].get("routed")),
        "canTakeOver": bool(call and call["state"] in ("active", "held", "dialing", "alerting")
                            and not (status["audio"] and status["audio"].get("routed"))),
        "hasCallerId": bool(call and call["number"]),
        "hasWidebandAudio": bool(status["transport"] and status["transport"].get("wideband")),
        "hasPhonebook": bool(index),
        "hasOperator": False,
        "hasSignal": False,
    }
    if mask_numbers:
        for c in status["calls"]:
            c["number"] = phone.mask(c["number"])
    return status


def print_status(status):
    p = status["phone"]
    bt = status["bluetooth"]
    print(f"Bluetooth:  {'on' if bt and bt['powered'] else 'off/unavailable'}")
    if not p:
        print("iPhone:     not paired (run: iphone-connect pair)")
    else:
        print(f"iPhone:     {p['name']} - {'connected' if p['connected'] else 'paired, not connected'}")
    print(f"Profile:    {'HFP (hands-free)' if status['hfp'] else 'HFP not connected'}")
    call = status["call"]
    print(f"Call state: {call['state'] if call else 'idle'}")
    if call:
        print(f"Caller:     {call['number'] or call['name'] or 'unknown (no caller ID)'}")
    t = status["transport"]
    if t:
        print(f"Audio link: {t['state'] or '-'}{' / ' + t['codec'] if t['codec'] else ''}")
    a = status["audio"]
    if a:
        print(f"Audio:      {'call audio active' if a['routed'] else 'no call audio'}{' (microphone muted)' if a.get('muted') else ''}")
        print(f"Microphone: {a['microphone']}")
        print(f"Noise supp: {'active' if a.get('noiseSuppression') else ('on (starts with the call)' if a.get('noiseSuppressionEnabled') else 'off')}")
        print(f"Output:     {a['output']}")
    for error in status["errors"]:
        print(f"Error:      {error}")


# ---- helpers ----------------------------------------------------------------------

def telephony_and_calls():
    tel = Telephony()
    ag_path, _ = tel.require_gateway()
    return tel, ag_path, tel.calls(ag_path)


def find_call(calls, states):
    for call in calls:
        if call["state"] in states:
            return call
    return None


# ---- commands ---------------------------------------------------------------------

def cmd_status(args):
    status = collect_status(mask_numbers=args.mask)
    if args.json:
        print(json.dumps(status))
    else:
        print_status(status)


def cmd_devices(args):
    devs = bluez.devices(bluez.system_bus(), phones_only=not args.all)
    if args.json:
        print(json.dumps(devs))
        return
    if not devs:
        print("No phones known to BlueZ. Pair one with: iphone-connect pair")
    for d in devs:
        flags = [f for f, on in (("paired", d["paired"]), ("trusted", d["trusted"]),
                                  ("connected", d["connected"]), ("HFP", d["hfp"])) if on]
        print(f"{d['address']}  {d['name']}  [{', '.join(flags) or 'not paired'}]")


def cmd_connect(args):
    bus = bluez.system_bus()
    device = bluez.find_phone(bus, args.address)
    print(f"Connecting to {device['name']} (HFP)...")
    bluez.connect(bus, device)
    tel = Telephony()
    loop = GLib.MainLoop()
    found = {"ok": tel.gateway(device["address"])[0] is not None}
    if not found["ok"]:
        def on_event(event):
            if event["type"] == "phone-connected":
                found["ok"] = True
                loop.quit()
        tel.subscribe(on_event)
        GLib.timeout_add_seconds(15, loop.quit)
        loop.run()
    if not found["ok"]:
        raise UserError("Bluetooth connected, but no HFP audio gateway appeared in PipeWire within 15 s")
    print("Connected. Calls can be made and received now.")


def cmd_disconnect(args):
    bus = bluez.system_bus()
    device = bluez.find_phone(bus, args.address)
    bluez.disconnect(bus, device)
    print(f"Disconnected {device['name']}.")


def cmd_call(args):
    number = phone.normalize(args.number)
    tel = Telephony()
    tel.set_reject_sco(False)  # this call belongs to the PC: accept its audio
    tel.dial(number)
    os.makedirs(audio.STATE_DIR, exist_ok=True)
    with open(LAST_NUMBER_FILE, "w") as handle:
        handle.write(number)
    print(f"Dialing {number} ...")


def cmd_redial(_args):
    try:
        with open(LAST_NUMBER_FILE) as handle:
            number = handle.read().strip()
    except FileNotFoundError:
        raise UserError("no number has been dialed from this PC yet")
    tel = Telephony()
    tel.set_reject_sco(False)
    tel.dial(number)
    print(f"Dialing {number} ...")


def cmd_take(_args):
    """Move the audio of a call running on the iPhone to the PC."""
    tel, _, calls = telephony_and_calls()
    if not find_call(calls, ("active", "dialing", "alerting", "held")):
        raise UserError("there is no call to take over")
    tel.set_reject_sco(False)
    tel.activate_audio()
    print("Call audio moved to the PC.")


def cmd_answer(_args):
    tel, _, calls = telephony_and_calls()
    call = find_call(calls, ANSWERABLE)
    if not call:
        raise UserError("there is no incoming call")
    tel.set_reject_sco(False)
    tel.answer(call["path"])
    print("Call answered.")


def cmd_reject(_args):
    tel, _, calls = telephony_and_calls()
    call = find_call(calls, ANSWERABLE)
    if not call:
        raise UserError("there is no incoming call")
    tel.hangup(call["path"])
    print("Call rejected.")


def cmd_hangup(_args):
    tel, _, calls = telephony_and_calls()
    live = [c for c in calls if c["state"] in ENDABLE]
    if not live:
        raise UserError("there is no call to hang up")
    for call in live:
        tel.hangup(call["path"])
    print("Call ended.")


def cmd_tones(args):
    if not phone.valid_dtmf(args.tones):
        raise UserError("tones may only contain 0-9, *, #, A-D")
    tel, _, calls = telephony_and_calls()
    if not find_call(calls, ("active",)):
        raise UserError("tones can only be sent during an active call")
    tel.send_tones(args.tones)


def cmd_mute(args):
    router = audio.AudioRouter()
    target = {"on": True, "off": False}.get(args.state, not router.is_muted())
    router.set_mute(target)
    if router.is_muted() != target:
        raise UserError("the phone audio stream did not change its mute state")
    journal(f"microphone {'muted' if target else 'unmuted'} for the call")
    print("Microphone muted." if target else "Microphone unmuted.")


def cmd_noise(args):
    config = audio.load_config()
    if args.state in ("on", "off"):
        config["noiseSuppression"] = args.state == "on"
        audio.save_config(config)
        router = audio.AudioRouter()
        state = router.load_state()
        if state and args.state == "off":
            router.remove_filter()
            # streams fall back to the default devices once the filter is gone
        elif state and args.state == "on":
            router.enhance(state.get("address"))
    enabled = audio.noise_suppression_enabled()
    print(f"Noise suppression: {'on' if enabled else 'off'}")


def cmd_ringtone(args):
    config = audio.load_config()
    if args.state in ("on", "off"):
        config["ringtone"] = args.state == "on"
        audio.save_config(config)
    if args.file:
        if not os.path.isfile(args.file):
            raise UserError(f"file not found: {args.file}")
        config["ringtoneFile"] = os.path.abspath(args.file)
        audio.save_config(config)
    enabled = config.get("ringtone", True) is not False
    print(f"PC ringtone: {'on' if enabled else 'off'} ({config.get('ringtoneFile', 'default')})")


def cmd_notifications(args):
    config = audio.load_config()
    if args.state in ("on", "off"):
        config["notifications"] = args.state == "on"
        audio.save_config(config)
    enabled = config.get("notifications", True) is not False
    print(f"Incoming call notifications: {'on' if enabled else 'off'}")


def cmd_contacts(args):
    if args.action == "sync":
        device = bluez.find_phone(bluez.system_bus())
        print(f"Downloading contacts from {device['name']} (PBAP)...")
        count = contacts.sync(device["address"])
        print(f"{count} contacts with phone numbers saved.")
    elif args.action == "list":
        entries = contacts.load_contacts()
        if args.json:
            print(json.dumps(entries))
        else:
            for c in entries:
                print(f"{c['name']}: {', '.join(c['numbers'])}")
    elif args.action == "lookup":
        name = contacts.lookup(args.number)
        print(name or "not in contacts")
    elif args.action == "clear":
        try:
            os.remove(contacts.CACHE_FILE)
        except FileNotFoundError:
            pass
        print("Contact cache deleted.")
    else:
        info = contacts.cache_info()
        if not info:
            print("No contacts synced yet (iphone-connect contacts sync).")
        else:
            import time
            print(f"{info['count']} contacts, synced {time.strftime('%Y-%m-%d %H:%M', time.localtime(info['updated']))}")


def cmd_history(args):
    if args.action == "sync":
        device = bluez.find_phone(bluez.system_bus())
        entries = contacts.sync_history(device["address"])
        print(f"{len(entries)} calls synced from the iPhone.")
        return
    if args.action == "seen":
        contacts.mark_seen()
        return
    data = contacts.load_history()
    if args.json:
        print(json.dumps({"entries": data["entries"], "unseenMissed": contacts.unseen_missed(data)}))
        return
    labels = {"missed": "missed  ", "received": "incoming", "dialed": "outgoing"}
    for e in data["entries"]:
        print(f"{e['time'].replace('T', ' ')}  {labels.get(e['type'], e['type'])}  {e['name'] or e['number']}")


def cmd_volume(args):
    if not 0 <= args.percent <= 100:
        raise UserError("volume must be between 0 and 100")
    Telephony().set_speaker_volume(round(args.percent * 15 / 100))
    print(f"Call volume set to {args.percent}%.")


def cmd_pair(args):
    bus = bluez.system_bus()
    adapter_path, props = bluez.adapter(bus)
    if not props.get("Powered"):
        bluez.set_property(bus, adapter_path, bluez.ADAPTER_IFACE, "Powered", GLib.Variant("b", True))

    def confirm(name, passkey):
        if passkey is None:
            question = f"Allow {name} to pair? [y/N] "
        else:
            question = f"\n{name} wants to pair.\nDoes the iPhone show the code {passkey}? [y/N] "
        try:
            return input(question).strip().lower() in ("y", "yes", "j", "ja")
        except EOFError:
            return False

    agent = bluez.PairingAgent(bus, confirm)
    agent.register()
    before = {d["address"] for d in bluez.devices(bus) if d["paired"]}
    bluez.set_property(bus, adapter_path, bluez.ADAPTER_IFACE, "Pairable", GLib.Variant("b", True))
    bluez.set_property(bus, adapter_path, bluez.ADAPTER_IFACE, "DiscoverableTimeout", GLib.Variant("u", args.timeout))
    bluez.set_property(bus, adapter_path, bluez.ADAPTER_IFACE, "Discoverable", GLib.Variant("b", True))
    name = props.get("Alias") or props.get("Name")
    print(f"This PC is visible as \"{name}\" for {args.timeout} s.")
    print("On the iPhone: Settings > Bluetooth > tap the PC's name. Confirm the code on both sides.")

    loop = GLib.MainLoop()
    result = {"device": None}

    def check():
        for d in bluez.devices(bus):
            if d["paired"] and d["address"] not in before:
                result["device"] = d
                loop.quit()
                return False
        return True

    GLib.timeout_add(1000, check)
    GLib.timeout_add_seconds(args.timeout, loop.quit)
    try:
        loop.run()
    finally:
        try:
            bluez.set_property(bus, adapter_path, bluez.ADAPTER_IFACE, "Discoverable", GLib.Variant("b", False))
        except bluez.BluetoothError:
            pass
        agent.unregister()

    device = result["device"]
    if not device:
        raise UserError("no new device was paired")
    bluez.set_property(bus, device["path"], bluez.DEVICE_IFACE, "Trusted", GLib.Variant("b", True))
    print(f"Paired and trusted: {device['name']} ({device['address']})")
    print("Next: iphone-connect connect")


def cmd_watch(args):
    """Print a JSON status line now and after every telephony/Bluetooth change (no polling)."""
    tel = Telephony()
    loop = GLib.MainLoop()
    pending = {"id": 0}

    def emit():
        pending["id"] = 0
        print(json.dumps(collect_status(mask_numbers=args.mask)), flush=True)
        return False

    def schedule(*_):
        if not pending["id"]:
            pending["id"] = GLib.timeout_add(150, emit)

    tel.subscribe(lambda _event: schedule())
    sysbus = bluez.system_bus()
    sysbus.signal_subscribe(bluez.BLUEZ, "org.freedesktop.DBus.Properties", "PropertiesChanged", None,
                            bluez.DEVICE_IFACE, Gio.DBusSignalFlags.NONE, schedule, None)

    # Mute changes of the call streams produce no D-Bus signal; PipeWire's
    # event stream (pactl subscribe) reports them, so every panel stays in sync.
    proc = None
    try:
        # --pdeathsig: pactl ends together with this process, even on SIGKILL
        proc = Gio.Subprocess.new(["setpriv", "--pdeathsig", "TERM", "pactl", "subscribe"], Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        reader = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def on_line(stream, result):
            line, _length = stream.read_line_finish_utf8(result)
            if line is None:
                return
            if "source-output" in line or "sink-input" in line:
                schedule()
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

        reader.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
    except GLib.Error as error:
        log(f"pactl subscribe unavailable: {error.message}")
    emit()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, loop.quit)
    try:
        loop.run()
    finally:
        if proc:
            proc.force_exit()


def cmd_daemon(_args):
    from .daemon import run
    run()


def cmd_diagnostics(args):
    from .diagnostics import report
    print(report(unmask=args.unmask))


# ---- main ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="iphone-connect", description="Use your iPhone for calls on Omarchy via Bluetooth HFP.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show phone, call and audio state")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mask", action="store_true", help="mask phone numbers")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("devices", help="list phones known to BlueZ")
    p.add_argument("--all", action="store_true", help="list all Bluetooth devices")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("pair", help="make this PC visible and pair an iPhone")
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(func=cmd_pair)

    for name, func, text in (("connect", cmd_connect, "connect the paired iPhone (HFP)"),
                             ("disconnect", cmd_disconnect, "disconnect the iPhone")):
        p = sub.add_parser(name, help=text)
        p.add_argument("address", nargs="?")
        p.set_defaults(func=func)

    p = sub.add_parser("call", help="dial a number")
    p.add_argument("number")
    p.set_defaults(func=cmd_call)

    for name, func, text in (("answer", cmd_answer, "answer the incoming call"),
                             ("reject", cmd_reject, "reject the incoming call"),
                             ("hangup", cmd_hangup, "end the current call"),
                             ("redial", cmd_redial, "dial the last number dialed from this PC")):
        sub.add_parser(name, help=text).set_defaults(func=func)
    sub.add_parser("take", help="move a call running on the iPhone to the PC").set_defaults(func=cmd_take)

    p = sub.add_parser("tones", help="send DTMF tones during a call")
    p.add_argument("tones")
    p.set_defaults(func=cmd_tones)

    p = sub.add_parser("mute", help="mute the PC microphone for the call")
    p.add_argument("state", nargs="?", choices=["on", "off", "toggle"], default="toggle")
    p.set_defaults(func=cmd_mute)

    p = sub.add_parser("noise", help="WebRTC noise suppression + echo cancellation for calls")
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p.set_defaults(func=cmd_noise)

    p = sub.add_parser("ringtone", help="ring on the PC for incoming calls")
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p.add_argument("--file", help="custom ringtone (any format pw-play can read)")
    p.set_defaults(func=cmd_ringtone)

    p = sub.add_parser("notifications", help="desktop notification for incoming calls")
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p.set_defaults(func=cmd_notifications)

    p = sub.add_parser("contacts", help="caller names from the iPhone phonebook (PBAP)")
    p.add_argument("action", nargs="?", choices=["status", "sync", "list", "lookup", "clear"], default="status")
    p.add_argument("number", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("history", help="recent and missed calls from the iPhone (PBAP)")
    p.add_argument("action", nargs="?", choices=["list", "sync", "seen"], default="list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("volume", help="set call volume on the phone (0-100)")
    p.add_argument("percent", type=int)
    p.set_defaults(func=cmd_volume)

    p = sub.add_parser("watch", help="stream JSON status lines (used by the Omarchy plugin)")
    p.add_argument("--mask", action="store_true")
    p.set_defaults(func=cmd_watch)

    sub.add_parser("daemon", help="run the background service (audio routing, notifications)").set_defaults(func=cmd_daemon)

    p = sub.add_parser("diagnostics", help="collect diagnostic information")
    p.add_argument("--unmask", action="store_true", help="do not mask phone numbers and addresses")
    p.set_defaults(func=cmd_diagnostics)
    return parser


ACTIONS = {"take", "noise", "call", "redial", "answer", "reject", "hangup", "tones", "mute", "volume", "connect", "disconnect"}


def journal(message):
    """Log user actions to the journal (visible with: journalctl --user -t iphone-connect)."""
    try:
        import syslog
        syslog.openlog("iphone-connect", 0, syslog.LOG_USER)
        syslog.syslog(message)
    except OSError:
        pass


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in ACTIONS and getattr(args, "state", None) != "status":
        detail = getattr(args, "state", None) or getattr(args, "percent", None)
        journal(f"action: {args.command}{' ' + str(detail) if detail is not None else ''}")
    try:
        args.func(args)
    except (UserError, phone.InvalidNumber, bluez.BluetoothError, TelephonyError, RuntimeError) as error:
        print(f"iphone-connect: {error}", file=sys.stderr)
        if args.command in ACTIONS:
            journal(f"action {args.command} failed: {error}")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
