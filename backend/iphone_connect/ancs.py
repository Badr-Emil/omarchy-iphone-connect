"""Apple Notification Center Service and Apple Media Service messages.

Encoding and decoding only; no I/O. Both services are documented by Apple
("ANCS Specification", "AMS Specification").
"""

import struct

# ---- ANCS -------------------------------------------------------------------------

SERVICE = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIFICATION_SOURCE = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"

EVENT_ADDED, EVENT_MODIFIED, EVENT_REMOVED = 0, 1, 2

FLAG_SILENT = 1 << 0
FLAG_IMPORTANT = 1 << 1
FLAG_PREEXISTING = 1 << 2
FLAG_POSITIVE_ACTION = 1 << 3
FLAG_NEGATIVE_ACTION = 1 << 4

CATEGORIES = ("other", "incoming-call", "missed-call", "voicemail", "social", "schedule", "email", "news",
              "health", "finance", "location", "entertainment")

COMMAND_GET_NOTIFICATION_ATTRIBUTES = 0
COMMAND_GET_APP_ATTRIBUTES = 1
COMMAND_PERFORM_ACTION = 2

ATTR_APP_ID, ATTR_TITLE, ATTR_SUBTITLE, ATTR_MESSAGE, ATTR_MESSAGE_SIZE, ATTR_DATE = 0, 1, 2, 3, 4, 5
APP_ATTR_DISPLAY_NAME = 0
ACTION_POSITIVE, ACTION_NEGATIVE = 0, 1

# What is asked of the phone for every notification: (attribute, maximum length or None)
NOTIFICATION_ATTRIBUTES = ((ATTR_APP_ID, None), (ATTR_TITLE, 96), (ATTR_SUBTITLE, 96), (ATTR_MESSAGE, 400),
                           (ATTR_DATE, None))


def category_name(category):
    return CATEGORIES[category] if 0 <= category < len(CATEGORIES) else "other"


def parse_event(value):
    """Notification Source packet -> dict, or None when it is malformed."""
    if len(value) < 8:
        return None
    event, flags, category, count, uid = struct.unpack_from("<BBBBI", value)
    if event > EVENT_REMOVED:
        return None
    return {
        "event": ("added", "modified", "removed")[event],
        "uid": uid,
        "category": category_name(category),
        "categoryCount": count,
        "silent": bool(flags & FLAG_SILENT),
        "important": bool(flags & FLAG_IMPORTANT),
        "preexisting": bool(flags & FLAG_PREEXISTING),
        "canDismiss": bool(flags & FLAG_NEGATIVE_ACTION),
        "canAct": bool(flags & FLAG_POSITIVE_ACTION),
    }


def get_notification_attributes(uid, attributes=NOTIFICATION_ATTRIBUTES):
    command = struct.pack("<BI", COMMAND_GET_NOTIFICATION_ATTRIBUTES, uid)
    for attribute, limit in attributes:
        command += bytes([attribute])
        if limit is not None:
            command += struct.pack("<H", limit)
    return command


def get_app_attributes(app_id):
    return bytes([COMMAND_GET_APP_ATTRIBUTES]) + app_id.encode("utf-8") + b"\x00" + bytes([APP_ATTR_DISPLAY_NAME])


def perform_action(uid, action):
    return struct.pack("<BIB", COMMAND_PERFORM_ACTION, uid, action)


def _attributes(data, offset, expected):
    """Read `expected` (id, length, value) tuples; None while the answer is incomplete."""
    found = {}
    for _ in range(expected):
        if offset + 3 > len(data):
            return None
        attribute, length = struct.unpack_from("<BH", data, offset)
        offset += 3
        if offset + length > len(data):
            return None
        found[attribute] = data[offset:offset + length].decode("utf-8", "replace")
        offset += length
    return found


def parse_notification_attributes(data, expected=len(NOTIFICATION_ATTRIBUTES)):
    """Data Source answer to get_notification_attributes.

    The phone splits long answers over several packets; pass everything
    received so far. Returns (uid, {attribute: text}) or None while incomplete.
    """
    if len(data) < 5 or data[0] != COMMAND_GET_NOTIFICATION_ATTRIBUTES:
        return None
    uid = struct.unpack_from("<I", data, 1)[0]
    attributes = _attributes(data, 5, expected)
    return None if attributes is None else (uid, attributes)


def parse_app_attributes(data, expected=1):
    """Data Source answer to get_app_attributes -> (app id, {attribute: text}) or None."""
    if len(data) < 2 or data[0] != COMMAND_GET_APP_ATTRIBUTES:
        return None
    end = data.find(b"\x00", 1)
    if end == -1:
        return None
    attributes = _attributes(data, end + 1, expected)
    return None if attributes is None else (data[1:end].decode("utf-8", "replace"), attributes)


def parse_date(text):
    """ANCS dates look like 20261002T094118; return ISO 8601 or ""."""
    if len(text) == 15 and text[8] == "T" and (text[:8] + text[9:]).isdigit():
        return "%s-%s-%sT%s:%s:%s" % (text[0:4], text[4:6], text[6:8], text[9:11], text[11:13], text[13:15])
    return ""


# ---- AMS --------------------------------------------------------------------------

MEDIA_SERVICE = "89d3502b-0f36-433a-8ef4-c502ad55f8dc"
REMOTE_COMMAND = "9b3c81d8-57b1-4a8a-b8df-0e56f7ca51c2"
ENTITY_UPDATE = "2f7cabce-808d-411f-9a0c-bb92ba96c102"

ENTITY_PLAYER, ENTITY_QUEUE, ENTITY_TRACK = 0, 1, 2
PLAYER_NAME, PLAYER_PLAYBACK_INFO = 0, 1
TRACK_ARTIST, TRACK_ALBUM, TRACK_TITLE, TRACK_DURATION = 0, 1, 2, 3

MEDIA_COMMANDS = {"play": 0, "pause": 1, "toggle": 2, "next": 3, "previous": 4}

# Written to Entity Update to say which values the phone should report
MEDIA_SUBSCRIPTIONS = (bytes([ENTITY_PLAYER, PLAYER_NAME, PLAYER_PLAYBACK_INFO]),
                       bytes([ENTITY_TRACK, TRACK_ARTIST, TRACK_ALBUM, TRACK_TITLE]))


def media_command(name):
    return bytes([MEDIA_COMMANDS[name]])


def parse_media_update(value):
    """Entity Update packet -> {field: value} for the media state, or {}."""
    if len(value) < 3:
        return {}
    entity, attribute = value[0], value[1]
    text = value[3:].decode("utf-8", "replace")
    if entity == ENTITY_TRACK:
        field = {TRACK_ARTIST: "artist", TRACK_ALBUM: "album", TRACK_TITLE: "title"}.get(attribute)
        return {field: text} if field else {}
    if entity == ENTITY_PLAYER:
        if attribute == PLAYER_NAME:
            return {"player": text}
        if attribute == PLAYER_PLAYBACK_INFO:
            # "state,rate,elapsed": 0 paused, 1 playing, 2 rewinding, 3 fast forwarding
            state = text.split(",")[0]
            return {"playing": state in ("1", "2", "3")}
    return {}


def parse_supported_commands(value):
    """Remote Command notification: the commands the current player accepts."""
    names = {number: name for name, number in MEDIA_COMMANDS.items()}
    return sorted(names[number] for number in value if number in names)
