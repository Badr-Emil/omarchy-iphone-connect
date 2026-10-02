"""D-Bus entry point of the background service (session bus).

The service holds the phone's single data channel, so the panel and the CLI
ask it to act on notifications and media instead of opening a channel of
their own.
"""

from gi.repository import Gio, GLib

NAME = "io.github.badr_emil.IPhoneConnect"
PATH = "/io/github/badr_emil/IPhoneConnect"
IFACE = "io.github.badr_emil.IPhoneConnect1"

XML = """
<node>
  <interface name="io.github.badr_emil.IPhoneConnect1">
    <method name="Dismiss"><arg type="u" direction="in"/><arg type="b" direction="out"/></method>
    <method name="DismissAll"><arg type="u" direction="out"/></method>
    <method name="Media"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  </interface>
</node>
"""


class ControlError(RuntimeError):
    pass


class Control:
    """Server side: forwards calls to the Mirror."""

    def __init__(self, bus, mirror):
        self.bus = bus
        self.mirror = mirror
        self.registration = 0
        self.owner = 0

    def register(self):
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self.registration = self.bus.register_object(PATH, info, self._handle, None, None)
        self.owner = Gio.bus_own_name_on_connection(self.bus, NAME, Gio.BusNameOwnerFlags.NONE, None, None)

    def unregister(self):
        if self.owner:
            Gio.bus_unown_name(self.owner)
            self.owner = 0
        if self.registration:
            self.bus.unregister_object(self.registration)
            self.registration = 0

    def _handle(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "Dismiss":
            done = self.mirror.dismiss(params.unpack()[0])
            invocation.return_value(GLib.Variant("(b)", (done,)))
        elif method == "DismissAll":
            uids = [n["uid"] for n in self.mirror.store.listing() if n.get("canDismiss")]
            count = sum(1 for uid in uids if self.mirror.dismiss(uid))
            invocation.return_value(GLib.Variant("(u)", (count,)))
        elif method == "Media":
            done = self.mirror.media(params.unpack()[0])
            invocation.return_value(GLib.Variant("(b)", (done,)))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)


def call(method, args=None, reply="(b)"):
    """Client side, used by the CLI."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(NAME, PATH, IFACE, method, args, GLib.VariantType(reply),
                               Gio.DBusCallFlags.NO_AUTO_START, 8000, None)
    except GLib.Error as error:
        text = error.message or str(error)
        if "ServiceUnknown" in text or "NameHasNoOwner" in text:
            raise ControlError("the background service is not running (systemctl --user start iphone-connect)")
        raise ControlError(text.split(":", 2)[-1].strip() or text)
    return result.unpack()[0]
