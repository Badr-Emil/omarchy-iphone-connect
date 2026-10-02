"""Minimal ATT client (Bluetooth Attribute Protocol), without any I/O.

The iPhone offers its GATT services over the classic Bluetooth link that the
hands-free profile already uses (L2CAP PSM 31). BlueZ does not attach a GATT
client to that link, so this module speaks the few ATT operations needed:
service and characteristic discovery, descriptor lookup, writes and
notifications.

Nothing here touches a socket. Whoever owns the channel passes received
packets to AttClient.feed() and gives it a function that sends packets, which
keeps the protocol testable against a simulated phone.
"""

import struct
import uuid

PSM = 31

OP_ERROR = 0x01
OP_MTU_REQ = 0x02
OP_MTU_RSP = 0x03
OP_FIND_INFO_REQ = 0x04
OP_FIND_INFO_RSP = 0x05
OP_READ_BY_TYPE_REQ = 0x08
OP_READ_BY_TYPE_RSP = 0x09
OP_READ_REQ = 0x0A
OP_READ_RSP = 0x0B
OP_READ_BY_GROUP_REQ = 0x10
OP_READ_BY_GROUP_RSP = 0x11
OP_WRITE_REQ = 0x12
OP_WRITE_RSP = 0x13
OP_NOTIFY = 0x1B
OP_INDICATE = 0x1D
OP_CONFIRM = 0x1E

ERR_READ_NOT_PERMITTED = 0x02
ERR_WRITE_NOT_PERMITTED = 0x03
ERR_INSUFFICIENT_AUTHENTICATION = 0x05
ERR_REQUEST_NOT_SUPPORTED = 0x06
ERR_INSUFFICIENT_AUTHORIZATION = 0x08
ERR_ATTRIBUTE_NOT_FOUND = 0x0A
ERR_INSUFFICIENT_ENCRYPTION = 0x0F

ERROR_NAMES = {
    ERR_READ_NOT_PERMITTED: "read not permitted",
    ERR_WRITE_NOT_PERMITTED: "write not permitted",
    ERR_INSUFFICIENT_AUTHENTICATION: "insufficient authentication",
    ERR_REQUEST_NOT_SUPPORTED: "request not supported",
    ERR_INSUFFICIENT_AUTHORIZATION: "insufficient authorization",
    ERR_ATTRIBUTE_NOT_FOUND: "attribute not found",
    ERR_INSUFFICIENT_ENCRYPTION: "insufficient encryption",
}
# The phone answers with one of these until the user allows the PC to see
# its notifications.
PERMISSION_ERRORS = (ERR_INSUFFICIENT_AUTHENTICATION, ERR_INSUFFICIENT_AUTHORIZATION, ERR_INSUFFICIENT_ENCRYPTION)

UUID_PRIMARY_SERVICE = 0x2800
UUID_CHARACTERISTIC = 0x2803
UUID_CCCD = 0x2902   # Client Characteristic Configuration: switches notifications on

BASE_UUID = "0000%04x-0000-1000-8000-00805f9b34fb"


class AttError(Exception):
    def __init__(self, code, handle=0, opcode=0):
        super().__init__(ERROR_NAMES.get(code, "ATT error 0x%02x" % code))
        self.code = code
        self.handle = handle
        self.opcode = opcode

    @property
    def needs_permission(self):
        return self.code in PERMISSION_ERRORS


def uuid_from_wire(raw):
    """16-bit and 128-bit UUIDs travel little-endian; return the usual text form."""
    if len(raw) == 2:
        return BASE_UUID % struct.unpack("<H", raw)[0]
    if len(raw) == 16:
        return str(uuid.UUID(bytes=bytes(reversed(raw))))
    raise ValueError("UUID of %d bytes" % len(raw))


def uuid_to_wire(value):
    if isinstance(value, int):
        return struct.pack("<H", value)
    return bytes(reversed(uuid.UUID(value).bytes))


def is_response(opcode):
    return opcode in (OP_ERROR, OP_MTU_RSP, OP_FIND_INFO_RSP, 0x07, OP_READ_BY_TYPE_RSP, OP_READ_RSP,
                      0x0D, 0x0F, OP_READ_BY_GROUP_RSP, OP_WRITE_RSP, 0x17, 0x19)


def is_command(opcode):
    """Commands (bit 6 set) expect no answer."""
    return bool(opcode & 0x40)


def read_by_group_request(start, end=0xFFFF, group=UUID_PRIMARY_SERVICE):
    return struct.pack("<BHH", OP_READ_BY_GROUP_REQ, start, end) + uuid_to_wire(group)


def read_by_type_request(start, end, kind=UUID_CHARACTERISTIC):
    return struct.pack("<BHH", OP_READ_BY_TYPE_REQ, start, end) + uuid_to_wire(kind)


def find_information_request(start, end):
    return struct.pack("<BHH", OP_FIND_INFO_REQ, start, end)


def write_request(handle, value):
    return struct.pack("<BH", OP_WRITE_REQ, handle) + bytes(value)


def error_response(opcode, handle, code):
    return struct.pack("<BBHB", OP_ERROR, opcode, handle, code)


def parse_services(pdu):
    """Read By Group Type response -> [(first handle, last handle, uuid)]."""
    size = pdu[1]
    if size not in (6, 20):
        raise ValueError("service entry of %d bytes" % size)
    services = []
    for offset in range(2, len(pdu) - size + 1, size):
        first, last = struct.unpack_from("<HH", pdu, offset)
        services.append((first, last, uuid_from_wire(pdu[offset + 4:offset + size])))
    return services


def parse_characteristics(pdu):
    """Read By Type response -> [(declaration handle, properties, value handle, uuid)]."""
    size = pdu[1]
    if size not in (7, 21):
        raise ValueError("characteristic entry of %d bytes" % size)
    result = []
    for offset in range(2, len(pdu) - size + 1, size):
        declaration, properties, value = struct.unpack_from("<HBH", pdu, offset)
        result.append((declaration, properties, value, uuid_from_wire(pdu[offset + 5:offset + size])))
    return result


def parse_descriptors(pdu):
    """Find Information response -> [(handle, uuid)]."""
    size = {1: 4, 2: 18}.get(pdu[1])
    if size is None:
        raise ValueError("unknown descriptor format %d" % pdu[1])
    result = []
    for offset in range(2, len(pdu) - size + 1, size):
        handle = struct.unpack_from("<H", pdu, offset)[0]
        result.append((handle, uuid_from_wire(pdu[offset + 2:offset + size])))
    return result


class AttClient:
    """One request at a time, as ATT requires; notifications at any time.

    The phone is a client on the same channel too and sends requests of its
    own. This side offers no attributes, so those are answered with "attribute
    not found" instead of being mistaken for answers.
    """

    def __init__(self, send, on_notify=None, mtu=672):
        self._send = send
        self.on_notify = on_notify or (lambda handle, value: None)
        self.mtu = mtu
        self._queue = []
        self._pending = None

    # ---- requests ---------------------------------------------------------------

    def request(self, pdu, on_done):
        """Send a request. on_done(response, None) or on_done(None, AttError)."""
        self._queue.append((bytes(pdu), on_done))
        self._pump()

    @property
    def busy(self):
        return self._pending is not None or bool(self._queue)

    def fail_pending(self, error):
        """Give up on everything outstanding, e.g. after a timeout or disconnect."""
        waiting = ([self._pending] if self._pending else []) + self._queue
        self._pending, self._queue = None, []
        for _pdu, on_done in waiting:
            on_done(None, error)

    def _pump(self):
        if self._pending is None and self._queue:
            self._pending = self._queue.pop(0)
            self._send(self._pending[0])

    # ---- incoming ---------------------------------------------------------------

    def feed(self, pdu):
        if not pdu:
            return
        opcode = pdu[0]
        if opcode in (OP_NOTIFY, OP_INDICATE):
            if opcode == OP_INDICATE:
                self._send(bytes([OP_CONFIRM]))
            if len(pdu) >= 3:
                self.on_notify(struct.unpack_from("<H", pdu, 1)[0], bytes(pdu[3:]))
        elif is_response(opcode):
            self._response(pdu)
        elif opcode == OP_MTU_REQ:
            self._send(struct.pack("<BH", OP_MTU_RSP, self.mtu))
        elif not is_command(opcode):
            handle = struct.unpack_from("<H", pdu, 1)[0] if len(pdu) >= 3 else 0
            self._send(error_response(opcode, handle, ERR_ATTRIBUTE_NOT_FOUND))

    def _response(self, pdu):
        if self._pending is None:
            return  # late answer to a request that already timed out
        (request, on_done), self._pending = self._pending, None
        if pdu[0] == OP_ERROR and len(pdu) >= 5:
            if pdu[1] != request[0]:
                self._pending = (request, on_done)   # error for something else
                return
            on_done(None, AttError(pdu[4], struct.unpack_from("<H", pdu, 2)[0], pdu[1]))
        else:
            on_done(bytes(pdu), None)
        self._pump()

    # ---- discovery --------------------------------------------------------------

    def discover_services(self, on_done):
        """on_done([(first, last, uuid)], None) or on_done(None, error)."""
        found = []

        def step(start):
            def reply(pdu, error):
                if error is not None:
                    return on_done(found, None) if error.code == ERR_ATTRIBUTE_NOT_FOUND else on_done(None, error)
                try:
                    found.extend(parse_services(pdu))
                except (ValueError, struct.error, IndexError) as broken:
                    return on_done(None, broken)
                last = found[-1][1]
                if last >= 0xFFFF:
                    return on_done(found, None)
                step(last + 1)
            self.request(read_by_group_request(start), reply)

        step(1)

    def discover_characteristics(self, first, last, on_done):
        """on_done({uuid: {"value": handle, "properties": bits, "end": last handle}}, None)."""
        found = []

        def finish():
            result = {}
            for index, (declaration, properties, value, char_uuid) in enumerate(found):
                end = found[index + 1][0] - 1 if index + 1 < len(found) else last
                result[char_uuid] = {"value": value, "properties": properties, "end": end}
            on_done(result, None)

        def step(start):
            if start > last:
                return finish()

            def reply(pdu, error):
                if error is not None:
                    return finish() if error.code == ERR_ATTRIBUTE_NOT_FOUND else on_done(None, error)
                try:
                    found.extend(parse_characteristics(pdu))
                except (ValueError, struct.error, IndexError) as broken:
                    return on_done(None, broken)
                step(found[-1][0] + 1)
            self.request(read_by_type_request(start, last), reply)

        step(first)

    def find_cccd(self, characteristic, on_done):
        """Handle of the descriptor that switches notifications on, or None."""
        start, end = characteristic["value"] + 1, characteristic["end"]

        def step(begin):
            if begin > end:
                return on_done(None, None)

            def reply(pdu, error):
                if error is not None:
                    return on_done(None, None if error.code == ERR_ATTRIBUTE_NOT_FOUND else error)
                try:
                    descriptors = parse_descriptors(pdu)
                except (ValueError, struct.error, IndexError) as broken:
                    return on_done(None, broken)
                for handle, descriptor in descriptors:
                    if descriptor == BASE_UUID % UUID_CCCD:
                        return on_done(handle, None)
                if not descriptors:
                    return on_done(None, None)
                step(descriptors[-1][0] + 1)
            self.request(find_information_request(begin, end), reply)

        step(start)

    def subscribe(self, characteristic, on_done):
        """Switch notifications on. on_done(None) or on_done(error)."""
        def found(handle, error):
            if error is not None:
                return on_done(error)
            if handle is None:
                return on_done(AttError(ERR_ATTRIBUTE_NOT_FOUND, characteristic["value"]))
            self.request(write_request(handle, b"\x01\x00"), lambda _pdu, failure: on_done(failure))
        self.find_cccd(characteristic, found)

    def write(self, handle, value, on_done=None):
        self.request(write_request(handle, value), lambda _pdu, error: on_done(error) if on_done else None)
