"""BlueZ (system bus) access: find the iPhone, connect HFP, pair with an agent."""

from gi.repository import Gio, GLib

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/io/github/iphone_connect/agent"

AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
    <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
    <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
    <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
</node>
"""


class BluetoothError(RuntimeError):
    pass


def system_bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    except GLib.Error as error:
        raise BluetoothError(f"cannot reach the system D-Bus: {error.message}")


def _call(bus, path, iface, method, args=None, reply=None, timeout=25000):
    try:
        return bus.call_sync(BLUEZ, path, iface, method, args,
                             GLib.VariantType(reply) if reply else None,
                             Gio.DBusCallFlags.NONE, timeout, None)
    except GLib.Error as error:
        message = error.message.split(":", 2)[-1].strip() if error.message else str(error)
        if "ServiceUnknown" in (error.message or ""):
            raise BluetoothError("BlueZ is not running (systemctl status bluetooth)")
        raise BluetoothError(message)


def managed_objects(bus):
    result = _call(bus, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", reply="(a{oa{sa{sv}}})")
    return result.unpack()[0]


def adapter(bus):
    for path, ifaces in managed_objects(bus).items():
        if ADAPTER_IFACE in ifaces:
            return path, ifaces[ADAPTER_IFACE]
    raise BluetoothError("no Bluetooth adapter found")


def is_phone(props):
    uuids = [u.lower() for u in props.get("UUIDs", [])]
    return HFP_AG_UUID in uuids or props.get("Icon") == "phone"


def devices(bus, phones_only=False):
    result = []
    for path, ifaces in managed_objects(bus).items():
        props = ifaces.get(DEVICE_IFACE)
        if not props:
            continue
        if phones_only and not is_phone(props):
            continue
        result.append({
            "path": path,
            "address": props.get("Address", ""),
            "name": props.get("Alias") or props.get("Name") or props.get("Address", ""),
            "paired": bool(props.get("Paired")),
            "trusted": bool(props.get("Trusted")),
            "connected": bool(props.get("Connected")),
            "phone": is_phone(props),
            "hfp": HFP_AG_UUID in [u.lower() for u in props.get("UUIDs", [])],
        })
    return sorted(result, key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))


def find_phone(bus, address=None):
    phones = [d for d in devices(bus, phones_only=True) if d["paired"]]
    if address:
        phones = [d for d in phones if d["address"].lower() == address.lower()]
    if not phones:
        raise BluetoothError("no paired iPhone found - run: iphone-connect pair")
    return phones[0]


def connect(bus, device):
    if not device["hfp"]:
        raise BluetoothError(f"{device['name']} does not offer the Hands-Free Audio Gateway profile")
    _call(bus, device["path"], DEVICE_IFACE, "ConnectProfile", GLib.Variant("(s)", (HFP_AG_UUID,)))


def disconnect(bus, device):
    _call(bus, device["path"], DEVICE_IFACE, "Disconnect")


def set_property(bus, path, iface, name, value):
    _call(bus, path, "org.freedesktop.DBus.Properties", "Set", GLib.Variant("(ssv)", (iface, name, value)))


class PairingAgent:
    """Interactive BlueZ agent: shows the passkey and asks for confirmation on stdin."""

    def __init__(self, bus, confirm):
        self.bus = bus
        self.confirm = confirm
        self.registration = None

    def register(self):
        info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
        self.registration = self.bus.register_object(AGENT_PATH, info, self._handle, None, None)
        _call(self.bus, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
              GLib.Variant("(os)", (AGENT_PATH, "DisplayYesNo")))
        _call(self.bus, "/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent",
              GLib.Variant("(o)", (AGENT_PATH,)))

    def unregister(self):
        try:
            _call(self.bus, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", GLib.Variant("(o)", (AGENT_PATH,)))
        except BluetoothError:
            pass
        if self.registration:
            self.bus.unregister_object(self.registration)
            self.registration = None

    def _device_name(self, path):
        for d in devices(self.bus):
            if d["path"] == path:
                return d["name"]
        return path

    def _reject(self, invocation):
        invocation.return_dbus_error("org.bluez.Error.Rejected", "Rejected by user")

    def _handle(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        if method == "RequestConfirmation":
            device, passkey = args
            if self.confirm(self._device_name(device), f"{passkey:06d}"):
                set_property(self.bus, device, DEVICE_IFACE, "Trusted", GLib.Variant("b", True))
                invocation.return_value(None)
            else:
                self._reject(invocation)
        elif method == "RequestAuthorization":
            if self.confirm(self._device_name(args[0]), None):
                invocation.return_value(None)
            else:
                self._reject(invocation)
        elif method == "AuthorizeService":
            device, uuid = args
            paired = any(d["path"] == device and d["paired"] for d in devices(self.bus))
            if paired:
                invocation.return_value(None)
            else:
                self._reject(invocation)
        elif method in ("DisplayPasskey", "DisplayPinCode"):
            print(f"Passkey shown on {self._device_name(args[0])}: {args[1]}", flush=True)
            invocation.return_value(None)
        elif method in ("RequestPinCode", "RequestPasskey"):
            # iPhones use numeric comparison; legacy PIN pairing is not supported here.
            self._reject(invocation)
        else:  # Release, Cancel
            invocation.return_value(None)
