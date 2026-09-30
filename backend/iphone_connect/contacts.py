"""Caller names from the iPhone's phonebook via Bluetooth PBAP (BlueZ obexd).

The contacts are downloaded once (and on demand) into a local cache that only
contains name + numbers, readable only by the user. Nothing leaves the PC.
"""

import json
import os
import re

from gi.repository import Gio, GLib

OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"
CLIENT_IFACE = "org.bluez.obex.Client1"
PBAP_IFACE = "org.bluez.obex.PhonebookAccess1"
TRANSFER_IFACE = "org.bluez.obex.Transfer1"

DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "iphone-connect")
CACHE_FILE = os.path.join(DATA_DIR, "contacts.json")
CALLLOG_FILE = os.path.join(DATA_DIR, "calllog.json")
MATCH_DIGITS = 9  # compare the last 9 digits: +43 660 1234567 == 0660 1234567


class ContactsError(RuntimeError):
    pass


# ---- vCard parsing / matching (pure, unit tested) ------------------------------------

def _unfold(text):
    # vCard line folding: CRLF followed by a space/tab continues the line
    return re.sub(r"\r?\n[ \t]", "", text)


def _decode(value):
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()


def parse_vcards(text):
    """Return [{"name": str, "numbers": [str]}] from vCard 2.1/3.0 text."""
    contacts = []
    current = None
    for line in _unfold(text).splitlines():
        key, _, value = line.partition(":")
        field = key.split(";")[0].upper()
        if field == "BEGIN":
            current = {"fn": "", "n": "", "numbers": []}
        elif field == "END" and current is not None:
            name = current["fn"] or current["n"]
            if name and current["numbers"]:
                contacts.append({"name": name, "numbers": current["numbers"]})
            current = None
        elif current is None:
            continue
        elif field == "FN":
            current["fn"] = _decode(value)
        elif field == "N":
            parts = [_decode(p) for p in value.split(";")]
            # N:Last;First;Middle;Prefix;Suffix -> "First Last"
            current["n"] = " ".join(p for p in (parts[1] if len(parts) > 1 else "", parts[0]) if p)
        elif field == "TEL" and value.strip():
            current["numbers"].append(value.strip())
    return contacts


_CALL_TYPES = {"MISSED": "missed", "RECEIVED": "received", "DIALED": "dialed"}


def parse_history(text):
    """Call history vCards -> [{"type", "time", "number", "name"}], newest first.

    "time" is ISO 8601 local time as sent by the phone (X-IRMC-CALL-DATETIME).
    """
    entries = []
    current = None
    for line in _unfold(text).splitlines():
        key, _, value = line.partition(":")
        parts = key.split(";")
        field = parts[0].upper()
        if field == "BEGIN":
            current = {"type": "", "time": "", "number": "", "name": ""}
        elif field == "END" and current is not None:
            if current["type"]:
                entries.append(current)
            current = None
        elif current is None:
            continue
        elif field == "X-IRMC-CALL-DATETIME":
            kind = next((_CALL_TYPES[p.upper()] for p in parts[1:] if p.upper() in _CALL_TYPES), "")
            current["type"] = kind
            m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", value.strip())
            if m:
                current["time"] = "{}-{}-{}T{}:{}:{}".format(*m.groups())
        elif field == "TEL" and not current["number"]:
            current["number"] = value.strip()
        elif field == "FN" and value.strip():
            current["name"] = _decode(value)
        elif field == "N" and not current["name"]:
            n = [_decode(p) for p in value.split(";")]
            current["name"] = " ".join(p for p in (n[1] if len(n) > 1 else "", n[0]) if p)
    entries.sort(key=lambda e: e["time"], reverse=True)
    return entries


def match_key(number):
    digits = re.sub(r"\D", "", str(number or ""))
    if len(digits) < 5:
        return digits  # short/service numbers must match exactly
    return digits[-MATCH_DIGITS:]


def build_index(contacts):
    index = {}
    for contact in contacts:
        for number in contact["numbers"]:
            key = match_key(number)
            if key:
                index.setdefault(key, contact["name"])
    return index


# ---- cache -----------------------------------------------------------------------------------

def save_cache(contacts, path=CACHE_FILE):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        entries = sorted(({"name": c["name"], "numbers": c["numbers"]} for c in contacts),
                         key=lambda c: c["name"].casefold())
        json.dump({"index": build_index(contacts), "count": len(contacts), "contacts": entries}, handle)
    os.replace(tmp, path)


def cache_info(path=CACHE_FILE):
    try:
        with open(path) as handle:
            count = json.load(handle).get("count", 0)
        return {"count": count, "updated": os.path.getmtime(path)}
    except (OSError, ValueError):
        return None


def load_contacts(path=CACHE_FILE):
    try:
        with open(path) as handle:
            return json.load(handle).get("contacts", [])
    except (OSError, ValueError):
        return []


def load_index(path=CACHE_FILE):
    try:
        with open(path) as handle:
            return json.load(handle).get("index", {})
    except (OSError, ValueError):
        return {}


def lookup(number, index=None):
    if not number:
        return ""
    index = load_index() if index is None else index
    return index.get(match_key(number), "")


# ---- PBAP download over obexd -----------------------------------------------------------------

def _bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        raise ContactsError(f"cannot reach the session D-Bus: {error.message}")


def _call(bus, path, iface, method, args=None, reply=None, timeout=60000):
    try:
        return bus.call_sync(OBEX, path, iface, method, args,
                             GLib.VariantType(reply) if reply else None,
                             Gio.DBusCallFlags.NONE, timeout, None)
    except GLib.Error as error:
        text = error.message or ""
        if "ServiceUnknown" in text or "not provided by any .service" in text:
            raise ContactsError("BlueZ OBEX service missing - install it: sudo pacman -S bluez-obex")
        if "Forbidden" in text or "Not Authorized" in text or "Unauthorized" in text:
            raise ContactsError("the iPhone refused access - on the iPhone open Settings > Bluetooth > (i) next "
                                "to this PC and turn on 'Sync Contacts'")
        raise ContactsError(text.split(":", 2)[-1].strip() or text)


def _pull(address, folder, fields=None, timeout=120, empty_message=None):
    """PullAll of one PBAP folder (pb, cch, ich, och, mch); returns vCard text."""
    bus = _bus()
    session = _call(bus, OBEX_PATH, CLIENT_IFACE, "CreateSession",
                    GLib.Variant("(sa{sv})", (address, {"Target": GLib.Variant("s", "pbap")})),
                    reply="(o)").unpack()[0]
    try:
        _call(bus, session, PBAP_IFACE, "Select", GLib.Variant("(ss)", ("int", folder)))
        size = _call(bus, session, PBAP_IFACE, "GetSize", None, reply="(q)").unpack()[0]
        if size == 0:
            if empty_message:
                raise ContactsError(empty_message)
            return ""
        filters = {"Format": GLib.Variant("s", "vcard30")}
        if fields:
            filters["Fields"] = GLib.Variant("as", fields)
        transfer, props = _call(bus, session, PBAP_IFACE, "PullAll",
                                GLib.Variant("(sa{sv})", ("", filters)), reply="(oa{sv})").unpack()
        filename = props.get("Filename")

        loop = GLib.MainLoop()
        result = {"status": None}

        def on_changed(_c, _s, path, _i, _m, params, _d):
            iface, changed, _inv = params.unpack()
            if path == transfer and iface == TRANSFER_IFACE and changed.get("Status") in ("complete", "error"):
                result["status"] = changed["Status"]
                loop.quit()

        sub = bus.signal_subscribe(OBEX, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                   transfer, None, Gio.DBusSignalFlags.NONE, on_changed, None)
        # the transfer may already be finished before we subscribed
        try:
            status = _call(bus, transfer, "org.freedesktop.DBus.Properties", "Get",
                           GLib.Variant("(ss)", (TRANSFER_IFACE, "Status")), reply="(v)").unpack()[0]
        except ContactsError:
            status = "complete"  # transfer objects vanish once they are done
        if status in ("complete", "error"):
            result["status"] = status
        else:
            GLib.timeout_add_seconds(timeout, loop.quit)
            loop.run()
        bus.signal_unsubscribe(sub)
        if result["status"] != "complete":
            raise ContactsError(f"download of '{folder}' did not finish")
        try:
            with open(filename, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        finally:
            try:
                os.remove(filename)
            except OSError:
                pass
    finally:
        try:
            _call(bus, OBEX_PATH, CLIENT_IFACE, "RemoveSession", GLib.Variant("(o)", (session,)))
        except ContactsError:
            pass


def download(address):
    """Download all contacts from the phone; returns the parsed contact list."""
    # iOS answers with an empty phonebook until contact sharing is allowed
    text = _pull(address, "pb", ["N", "FN", "TEL"],
                 empty_message="the iPhone shares no contacts - on the iPhone open Settings > Bluetooth > (i) "
                               "next to this PC and turn on 'Sync Contacts', then run: iphone-connect contacts sync")
    return parse_vcards(text)


HISTORY_FILE = os.path.join(DATA_DIR, "history.json")


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def load_history(path=HISTORY_FILE):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"entries": [], "seen": ""}


def sync_history(address, path=HISTORY_FILE):
    entries = parse_history(_pull(address, "cch", ["N", "FN", "TEL", "X-IRMC-CALL-DATETIME"]))
    index = load_index()
    for entry in entries:
        if not entry["name"]:
            entry["name"] = lookup(entry["number"], index)
    data = load_history(path)
    data["entries"] = entries
    _write_private(path, data)
    return entries


def mark_seen(path=HISTORY_FILE, calllog_path=CALLLOG_FILE):
    data = load_history(path)
    merged = merged_history(data, load_calllog(calllog_path))
    if merged["entries"]:
        data["seen"] = merged["entries"][0]["time"]
        _write_private(path, data)


CALLLOG_MAX = 500
MERGE_WINDOW = 120  # seconds: same number within 2 minutes = same call


def load_calllog(path=CALLLOG_FILE):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return []


def record_call(kind, number, name, when, path=CALLLOG_FILE):
    """Append a call seen live over HFP (the phone's PBAP log can lag behind)."""
    log = load_calllog(path)
    log.insert(0, {"type": kind, "time": when, "number": number or "", "name": name or ""})
    _write_private(path, log[:CALLLOG_MAX])


def _seconds(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def merged_history(data=None, calllog=None):
    """PBAP history plus live-recorded calls the phone did not report, newest first."""
    data = load_history() if data is None else data
    calllog = load_calllog() if calllog is None else calllog
    entries = list(data["entries"])
    for call in calllog:
        t = _seconds(call["time"])
        key = match_key(call["number"])
        duplicate = any(
            e["type"] == call["type"] and match_key(e["number"]) == key
            and t is not None and _seconds(e["time"]) is not None
            and abs(_seconds(e["time"]) - t) <= MERGE_WINDOW
            for e in data["entries"])
        if not duplicate:
            entries.append(dict(call))
    entries.sort(key=lambda e: e["time"], reverse=True)
    return {"entries": entries, "seen": data.get("seen", "")}


def unseen_missed(data):
    return sum(1 for e in data["entries"] if e["type"] == "missed" and e["time"] > data.get("seen", ""))


def sync(address):
    contacts = download(address)
    save_cache(contacts)
    return len(contacts)
