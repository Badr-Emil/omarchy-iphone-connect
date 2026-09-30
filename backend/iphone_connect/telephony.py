"""Client for PipeWire's HFP telephony service (org.pipewire.Telephony, session bus)."""

from gi.repository import Gio, GLib

from . import events
from .events import AG_IFACE, CALL_IFACE, TRANSPORT_IFACE

SERVICE = "org.pipewire.Telephony"
ROOT = "/org/pipewire/Telephony"


class TelephonyError(RuntimeError):
    pass


def _message(error):
    text = error.message or str(error)
    if "ServiceUnknown" in text or "NameHasNoOwner" in text:
        return "PipeWire telephony service is not running (check: systemctl --user status wireplumber)"
    # "GDBus.Error:org.pipewire.Telephony.Error.CME: ..." -> readable tail
    return text.split(":", 2)[-1].strip() or text


class Telephony:
    def __init__(self, bus=None):
        try:
            self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            raise TelephonyError(f"cannot reach the session D-Bus: {error.message}")

    def _call(self, path, iface, method, args=None, reply=None, timeout=15000):
        try:
            return self.bus.call_sync(SERVICE, path, iface, method, args,
                                      GLib.VariantType(reply) if reply else None,
                                      Gio.DBusCallFlags.NONE, timeout, None)
        except GLib.Error as error:
            raise TelephonyError(_message(error))

    # ---- queries -------------------------------------------------------------

    def _managed(self, path):
        result = self._call(path, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", reply="(a{oa{sa{sv}}})")
        return result.unpack()[0]

    def objects(self):
        """All telephony objects. The root ObjectManager only lists the phones
        (agN); each phone has its own ObjectManager that lists its calls."""
        objs = dict(self._managed(ROOT))
        for path, ifaces in list(objs.items()):
            if AG_IFACE in ifaces:
                try:
                    objs.update(self._managed(path))
                except TelephonyError:
                    pass
        return objs

    def gateways(self):
        objs = self.objects()
        return [(p, i) for p, i in sorted(objs.items()) if AG_IFACE in i]

    def gateway(self, address=None):
        for path, ifaces in self.gateways():
            if not address or ifaces[AG_IFACE].get("Address", "").lower() == address.lower():
                return path, ifaces
        return None, None

    def calls(self, ag_path=None):
        result = []
        for path, ifaces in sorted(self.objects().items()):
            if CALL_IFACE not in ifaces:
                continue
            if ag_path and events.ag_path_of(path) != ag_path:
                continue
            props = ifaces[CALL_IFACE]
            result.append({
                "path": path,
                "state": props.get("State", ""),
                "number": props.get("LineIdentification", ""),
                "name": props.get("Name", ""),
                "incoming": props.get("IncomingLine", ""),
                "multiparty": bool(props.get("Multiparty")),
            })
        return result

    def require_gateway(self, address=None):
        path, ifaces = self.gateway(address)
        if not path:
            raise TelephonyError("iPhone is not connected for calls (HFP) - run: iphone-connect connect")
        return path, ifaces

    # ---- actions -------------------------------------------------------------

    def dial(self, number, address=None):
        path, _ = self.require_gateway(address)
        self._call(path, AG_IFACE, "Dial", GLib.Variant("(s)", (number,)), timeout=30000)

    def answer(self, call_path):
        self._call(call_path, CALL_IFACE, "Answer")

    def hangup(self, call_path):
        self._call(call_path, CALL_IFACE, "Hangup")

    def hangup_all(self, address=None):
        path, _ = self.require_gateway(address)
        self._call(path, AG_IFACE, "HangupAll")

    def send_tones(self, tones, address=None):
        path, _ = self.require_gateway(address)
        self._call(path, AG_IFACE, "SendTones", GLib.Variant("(s)", (tones,)))

    def set_speaker_volume(self, level, address=None):
        """HFP volume scale is 0..15."""
        path, _ = self.require_gateway(address)
        self._call(path, "org.freedesktop.DBus.Properties", "Set",
                   GLib.Variant("(ssv)", (AG_IFACE, "SpeakerVolume", GLib.Variant("y", int(level)))))

    # ---- signals -------------------------------------------------------------

    def subscribe(self, callback):
        """Call callback(event_dict) for every telephony event. Returns subscription ids."""

        def on_signal(_conn, _sender, path, iface, member, params, _data):
            args = params.unpack()
            if member == "InterfacesAdded":
                evs = events.interfaces_added(args[0], args[1])
            elif member == "InterfacesRemoved":
                evs = events.interfaces_removed(args[0], args[1])
            elif member == "PropertiesChanged":
                evs = events.properties_changed(path, args[0], args[1])
            else:
                evs = []
            for event in evs:
                callback(event)

        ids = []
        for iface, member in (("org.freedesktop.DBus.ObjectManager", "InterfacesAdded"),
                              ("org.freedesktop.DBus.ObjectManager", "InterfacesRemoved"),
                              ("org.freedesktop.DBus.Properties", "PropertiesChanged")):
            ids.append(self.bus.signal_subscribe(SERVICE, iface, member, None, None,
                                                 Gio.DBusSignalFlags.NONE, on_signal, None))
        return ids

    def transport(self, ag_path):
        objs = self.objects()
        for path, ifaces in objs.items():
            if TRANSPORT_IFACE in ifaces and (path == ag_path or path.startswith(ag_path + "/")):
                props = ifaces[TRANSPORT_IFACE]
                return {"path": path, "state": props.get("State", ""),
                        "codec": events.codec_name(props.get("Codec")),
                        "wideband": events.is_wideband(props.get("Codec"))}
        return None
