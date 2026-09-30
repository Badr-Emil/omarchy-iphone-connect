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
        json.dump({"index": build_index(contacts), "count": len(contacts)}, handle)
    os.replace(tmp, path)


def cache_info(path=CACHE_FILE):
    try:
        with open(path) as handle:
            count = json.load(handle).get("count", 0)
        return {"count": count, "updated": os.path.getmtime(path)}
    except (OSError, ValueError):
        return None


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


def download(address, timeout=120):
    """Download all contacts from the phone; returns the parsed contact list."""
    bus = _bus()
    session = _call(bus, OBEX_PATH, CLIENT_IFACE, "CreateSession",
                    GLib.Variant("(sa{sv})", (address, {"Target": GLib.Variant("s", "pbap")})),
                    reply="(o)").unpack()[0]
    try:
        _call(bus, session, PBAP_IFACE, "Select", GLib.Variant("(ss)", ("int", "pb")))
        size = _call(bus, session, PBAP_IFACE, "GetSize", None, reply="(q)").unpack()[0]
        if size == 0:
            # iOS answers with an empty phonebook until contact sharing is allowed
            raise ContactsError("the iPhone shares no contacts - on the iPhone open Settings > Bluetooth > (i) "
                                "next to this PC and turn on 'Sync Contacts', then run: iphone-connect contacts sync")
        filters = {"Format": GLib.Variant("s", "vcard30"),
                   "Fields": GLib.Variant("as", ["N", "FN", "TEL"])}
        transfer, props = _call(bus, session, PBAP_IFACE, "PullAll",
                                GLib.Variant("(sa{sv})", ("", filters)), reply="(oa{sv})").unpack()
        filename = props.get("Filename")

        loop = GLib.MainLoop()
        result = {"status": None}

        def on_changed(_c, _s, path, _i, _m, params, _d):
            iface, changed, _inv = params.unpack()
            if path == transfer and iface == TRANSFER_IFACE and "Status" in changed:
                if changed["Status"] in ("complete", "error"):
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
            raise ContactsError("contact download did not finish")
        try:
            with open(filename, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        finally:
            try:
                os.remove(filename)
            except OSError:
                pass
        return parse_vcards(text)
    finally:
        try:
            _call(bus, OBEX_PATH, CLIENT_IFACE, "RemoveSession", GLib.Variant("(o)", (session,)))
        except ContactsError:
            pass


def sync(address):
    contacts = download(address)
    save_cache(contacts)
    return len(contacts)
