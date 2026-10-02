"""A simulated iPhone for the tests: an ATT server with Apple's notification
and media services, laid out like the real one (same handles, same quirks).

It answers discovery, hands out notification details in small fragments, and,
like a real iPhone, sends requests of its own on the same channel.
"""

import struct
from collections import deque

from iphone_connect import ancs, att

PRIMARY = att.BASE_UUID % att.UUID_PRIMARY_SERVICE
CHARACTERISTIC = att.BASE_UUID % att.UUID_CHARACTERISTIC
CCCD = att.BASE_UUID % att.UUID_CCCD

# (service uuid, first handle, last handle, [(characteristic uuid, properties, has cccd)])
LAYOUT = [
    (att.BASE_UUID % 0x1800, 1, 5, [(att.BASE_UUID % 0x2A00, 0x02, False)]),
    (att.BASE_UUID % 0x1801, 6, 9, [(att.BASE_UUID % 0x2A05, 0x20, True)]),
    (att.BASE_UUID % 0x180A, 10, 14, [(att.BASE_UUID % 0x2A29, 0x02, False)]),
    ("d0611e78-bbb4-4591-a5f8-487910ae4366", 15, 19, []),
    ("9fa480e0-4967-4542-9390-d343dc5d04ae", 20, 24, []),
    (att.BASE_UUID % 0x180F, 25, 28, [(att.BASE_UUID % 0x2A19, 0x12, True)]),
    (att.BASE_UUID % 0x1805, 29, 34, [(att.BASE_UUID % 0x2A2B, 0x12, True)]),
    (ancs.SERVICE, 35, 44, [(ancs.CONTROL_POINT, 0x88, False), (ancs.NOTIFICATION_SOURCE, 0x10, True),
                            (ancs.DATA_SOURCE, 0x10, True)]),
    (ancs.MEDIA_SERVICE, 45, 56, [(ancs.REMOTE_COMMAND, 0x18, True), (ancs.ENTITY_UPDATE, 0x18, True)]),
]


class SimPhone:
    def __init__(self, fragment=18, authorized=True, with_media=True, with_ancs=True):
        self.fragment = fragment          # bytes of notification detail per packet
        self.authorized = authorized
        self.outbox = deque()             # packets on their way to the computer
        self.attributes = {}              # handle -> (type uuid, value bytes)
        self.handles = {}                 # characteristic uuid -> value handle
        self.cccd = {}                    # cccd handle -> characteristic uuid
        self.subscribed = set()
        self.notifications = {}           # uid -> dict
        self.apps = {"com.apple.MobileSMS": "Messages", "net.whatsapp.WhatsApp": "WhatsApp"}
        self.dismissed = []
        self.media_commands = []
        self.media = {(ancs.ENTITY_PLAYER, ancs.PLAYER_NAME): "Music",
                      (ancs.ENTITY_PLAYER, ancs.PLAYER_PLAYBACK_INFO): "1,1.0,12.5",
                      (ancs.ENTITY_TRACK, ancs.TRACK_ARTIST): "Nina Simone",
                      (ancs.ENTITY_TRACK, ancs.TRACK_ALBUM): "Pastel Blues",
                      (ancs.ENTITY_TRACK, ancs.TRACK_TITLE): "Sinnerman"}
        self.requests_seen = []
        self.errors_from_computer = []
        self.ranges = {}                  # first handle of a service -> its last handle
        for service, first, last, characteristics in LAYOUT:
            if (service == ancs.SERVICE and not with_ancs) or (service == ancs.MEDIA_SERVICE and not with_media):
                continue
            self.attributes[first] = (PRIMARY, att.uuid_to_wire(_short(service)))
            handle = first + 1
            for uuid, properties, has_cccd in characteristics:
                value = handle + 1
                self.attributes[handle] = (CHARACTERISTIC, struct.pack("<BH", properties, value) + att.uuid_to_wire(_short(uuid)))
                self.attributes[value] = (uuid, b"")
                self.handles[uuid] = value
                handle = value + 1
                if properties & 0x80:   # extended properties descriptor, as on the real phone
                    self.attributes[handle] = (att.BASE_UUID % 0x2900, b"\x01\x00")
                    handle += 1
                if has_cccd:
                    self.attributes[handle] = (CCCD, b"\x00\x00")
                    self.cccd[handle] = uuid
                    handle += 1
            self.ranges[first] = last

    # ---- what a test does with the phone ---------------------------------------------

    def add(self, uid, app="com.apple.MobileSMS", title="Mum", message="Call me back", subtitle="",
            date="20261002T094118", category=4, flags=ancs.FLAG_NEGATIVE_ACTION, event=ancs.EVENT_ADDED):
        self.notifications[uid] = {"app": app, "title": title, "subtitle": subtitle, "message": message,
                                   "date": date, "category": category, "flags": flags}
        self._source_event(event, uid)

    def remove(self, uid):
        self._source_event(ancs.EVENT_REMOVED, uid)
        self.notifications.pop(uid, None)

    def ask_something(self):
        """Real iPhones browse the computer's services over the same channel."""
        self.outbox.append(att.read_by_group_request(1))

    def set_media(self, entity, attribute, text):
        self.media[(entity, attribute)] = text
        if ancs.ENTITY_UPDATE in self.subscribed:
            self._notify(ancs.ENTITY_UPDATE, bytes([entity, attribute, 0]) + text.encode())

    def _source_event(self, event, uid, extra_flags=0):
        if ancs.NOTIFICATION_SOURCE not in self.subscribed:
            return
        n = self.notifications[uid]
        self._notify(ancs.NOTIFICATION_SOURCE, struct.pack("<BBBBI", event, n["flags"] | extra_flags, n["category"],
                                                            len(self.notifications), uid))

    def _notify(self, uuid, value):
        self.outbox.append(struct.pack("<BH", att.OP_NOTIFY, self.handles[uuid]) + value)

    # ---- ATT server -------------------------------------------------------------------

    def receive(self, pdu):
        opcode = pdu[0]
        if opcode == att.OP_ERROR:
            self.errors_from_computer.append(pdu)
            return
        self.requests_seen.append(opcode)
        if opcode == att.OP_READ_BY_GROUP_REQ:
            start, end = struct.unpack_from("<HH", pdu, 1)
            self._listing(opcode, att.OP_READ_BY_GROUP_RSP, start, end, PRIMARY, grouped=True)
        elif opcode == att.OP_READ_BY_TYPE_REQ:
            start, end = struct.unpack_from("<HH", pdu, 1)
            self._listing(opcode, att.OP_READ_BY_TYPE_RSP, start, end, att.uuid_from_wire(pdu[5:]), grouped=False)
        elif opcode == att.OP_FIND_INFO_REQ:
            start, end = struct.unpack_from("<HH", pdu, 1)
            found = [(h, t) for h, (t, _v) in sorted(self.attributes.items()) if start <= h <= end]
            if not found:
                return self._error(opcode, start, att.ERR_ATTRIBUTE_NOT_FOUND)
            # one answer holds only 16-bit or only 128-bit UUIDs
            is_short = isinstance(_short(found[0][1]), int)
            entries = []
            for handle, kind in found[:4]:
                if isinstance(_short(kind), int) != is_short:
                    break
                entries.append(struct.pack("<H", handle) + att.uuid_to_wire(_short(kind)))
            self.outbox.append(bytes([att.OP_FIND_INFO_RSP, 1 if is_short else 2]) + b"".join(entries))
        elif opcode == att.OP_WRITE_REQ:
            self._write(struct.unpack_from("<H", pdu, 1)[0], pdu[3:])
        else:
            self._error(opcode, 0, att.ERR_REQUEST_NOT_SUPPORTED)

    def _error(self, opcode, handle, code):
        self.outbox.append(att.error_response(opcode, handle, code))

    def _listing(self, opcode, response, start, end, kind, grouped):
        entries = []
        for handle, (type_uuid, value) in sorted(self.attributes.items()):
            if start <= handle <= end and type_uuid == kind:
                entry = struct.pack("<H", handle) + (struct.pack("<H", self.ranges[handle]) if grouped else b"") + value
                if entries and len(entry) != len(entries[0]):
                    break      # entries of one answer must have the same size
                entries.append(entry)
                if len(entries) == 3:
                    break
        if not entries:
            return self._error(opcode, start, att.ERR_ATTRIBUTE_NOT_FOUND)
        self.outbox.append(bytes([response, len(entries[0])]) + b"".join(entries))

    def _write(self, handle, value):
        if handle in self.cccd:
            uuid = self.cccd[handle]
            if uuid in (ancs.NOTIFICATION_SOURCE, ancs.DATA_SOURCE) and not self.authorized:
                return self._error(att.OP_WRITE_REQ, handle, att.ERR_INSUFFICIENT_AUTHORIZATION)
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            if value[:1] == b"\x01":
                self.subscribed.add(uuid)
                if uuid == ancs.NOTIFICATION_SOURCE:   # everything already in the notification centre
                    for uid in sorted(self.notifications):
                        self._source_event(ancs.EVENT_ADDED, uid, ancs.FLAG_PREEXISTING)
                if uuid == ancs.REMOTE_COMMAND:
                    self._notify(ancs.REMOTE_COMMAND, bytes([0, 1, 2, 3, 4]))
            else:
                self.subscribed.discard(uuid)
        elif handle == self.handles.get(ancs.CONTROL_POINT):
            self._control_point(handle, value)
        elif handle == self.handles.get(ancs.ENTITY_UPDATE):
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            for attribute in value[1:]:
                text = self.media.get((value[0], attribute), "")
                self._notify(ancs.ENTITY_UPDATE, bytes([value[0], attribute, 0]) + text.encode())
        elif handle == self.handles.get(ancs.REMOTE_COMMAND):
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            self.media_commands.append(value[0])
        else:
            self._error(att.OP_WRITE_REQ, handle, att.ERR_WRITE_NOT_PERMITTED)

    def _control_point(self, handle, value):
        command = value[0]
        if command == ancs.COMMAND_GET_NOTIFICATION_ATTRIBUTES:
            uid = struct.unpack_from("<I", value, 1)[0]
            n = self.notifications.get(uid)
            if n is None:
                return self._error(att.OP_WRITE_REQ, handle, 0xA2)   # ANCS: invalid parameter
            answer, offset = value[:5], 5
            while offset < len(value):
                attribute = value[offset]
                offset += 1
                limit = None
                if attribute in (ancs.ATTR_TITLE, ancs.ATTR_SUBTITLE, ancs.ATTR_MESSAGE):
                    limit = struct.unpack_from("<H", value, offset)[0]
                    offset += 2
                text = {ancs.ATTR_APP_ID: n["app"], ancs.ATTR_TITLE: n["title"], ancs.ATTR_SUBTITLE: n["subtitle"],
                        ancs.ATTR_MESSAGE: n["message"], ancs.ATTR_DATE: n["date"]}.get(attribute, "").encode()
                text = text[:limit] if limit is not None else text
                answer += struct.pack("<BH", attribute, len(text)) + text
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            self._fragments(answer)
        elif command == ancs.COMMAND_GET_APP_ATTRIBUTES:
            app = value[1:value.index(b"\x00", 1)].decode()
            if app not in self.apps:
                return self._error(att.OP_WRITE_REQ, handle, 0xA2)
            name = self.apps[app].encode()
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            self._fragments(bytes([command]) + app.encode() + b"\x00" + struct.pack("<BH", 0, len(name)) + name)
        elif command == ancs.COMMAND_PERFORM_ACTION:
            uid, action = struct.unpack_from("<IB", value, 1)
            self.outbox.append(bytes([att.OP_WRITE_RSP]))
            if action == ancs.ACTION_NEGATIVE and uid in self.notifications:
                self.dismissed.append(uid)
                self.remove(uid)
        else:
            self._error(att.OP_WRITE_REQ, handle, 0xA0)

    def _fragments(self, data):
        for offset in range(0, len(data), self.fragment):
            self._notify(ancs.DATA_SOURCE, data[offset:offset + self.fragment])


def _short(uuid):
    """16-bit number for Bluetooth SIG UUIDs, the text itself for 128-bit ones."""
    if uuid.startswith("0000") and uuid.endswith("-0000-1000-8000-00805f9b34fb"):
        return int(uuid[4:8], 16)
    return uuid


def connect(session_factory, phone):
    """Wire a Session to a SimPhone. Returns (session, pump); pump() delivers
    packets in both directions until nobody has anything left to say."""
    to_phone = deque()
    session = session_factory(to_phone.append)

    def pump(limit=10000):
        for _ in range(limit):
            if to_phone:
                phone.receive(to_phone.popleft())
            elif phone.outbox:
                session.feed(phone.outbox.popleft())
            else:
                return
        raise AssertionError("phone and computer never stop talking")

    return session, pump
