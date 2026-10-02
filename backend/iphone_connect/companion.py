"""The iPhone's notifications and media state, read over the hands-free link.

Session drives the conversation with the phone: find Apple's notification
service (ANCS) and media service (AMS), subscribe, and turn what arrives into
plain dictionaries. It does no I/O of its own: the owner hands it received
packets and a send function, and calls tick() now and then so that a phone
that stops answering is noticed.
"""

import time

from . import ancs
from .att import AttClient, AttError

REQUEST_TIMEOUT = 12   # seconds without an answer before the link counts as dead
COMMAND_TIMEOUT = 6    # seconds to wait for the details of one notification
MAX_QUEUED = 300


def fallback_app_name(app_id):
    """com.burbn.instagram -> Instagram, for apps whose name the phone will not tell."""
    tail = app_id.rsplit(".", 1)[-1] if app_id else ""
    return tail[:1].upper() + tail[1:] if tail else "iPhone"


class Session:
    """States: discovering, ready, needs-permission, unsupported, failed."""

    def __init__(self, send, on_state=None, on_notification=None, on_removed=None, on_media=None, clock=time.monotonic):
        self.att = AttClient(self._send_packet, self._on_notify)
        self._send = send
        self._clock = clock
        self.on_state = on_state or (lambda state, detail: None)
        self.on_notification = on_notification or (lambda notification: None)
        self.on_removed = on_removed or (lambda uid: None)
        self.on_media = on_media or (lambda media: None)

        self.state = "discovering"
        self.detail = ""
        self.media = {"available": False, "player": "", "title": "", "artist": "", "album": "",
                      "playing": False, "commands": []}
        self._handles = {}        # characteristic uuid -> value handle
        self._media_range = None  # handle range of the media service, if the phone has one
        self._events = {}         # uid -> latest Notification Source event
        self._app_names = {}      # app id -> display name
        self._waiting = {}        # app id -> notifications held until its name is known
        self._commands = []       # control point commands still to send
        self._current = None      # (kind, key, deadline)
        self._data = b""
        self._last_sent = None

    # ---- lifecycle --------------------------------------------------------------

    def start(self):
        self.att.discover_services(self._services_found)

    def feed(self, packet):
        self.att.feed(packet)

    def tick(self):
        """Call every few seconds. Returns False once the link should be dropped."""
        now = self._clock()
        if self._current and now > self._current[2]:
            self._finish_command()    # the phone never sent the details; move on
        if self.att.busy and self._last_sent is not None and now - self._last_sent > REQUEST_TIMEOUT:
            self._fail("the phone stopped answering")
        return self.state != "failed"

    def retry(self):
        """Ask again after the phone said the computer is not allowed yet."""
        if self.state == "needs-permission":
            self.state, self.detail = "discovering", ""
            self.start()

    def close(self):
        self.att.fail_pending(AttError(0, 0, 0))

    def _send_packet(self, packet):
        self._last_sent = self._clock()
        self._send(packet)

    def _set_state(self, state, detail=""):
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self.on_state(state, detail)

    def _fail(self, detail):
        self._set_state("failed", detail)

    # ---- discovery --------------------------------------------------------------

    def _services_found(self, services, error):
        if error is not None:
            return self._fail("service discovery failed: %s" % error)
        ranges = {uuid: (first, last) for first, last, uuid in services}
        self._media_range = ranges.get(ancs.MEDIA_SERVICE)
        if ancs.SERVICE not in ranges:
            return self._set_state("unsupported", "this phone offers no notification service")
        first, last = ranges[ancs.SERVICE]
        self.att.discover_characteristics(first, last, self._notification_service_found)

    def _notification_service_found(self, characteristics, error):
        if error is not None:
            return self._fail("characteristic discovery failed: %s" % error)
        needed = (ancs.NOTIFICATION_SOURCE, ancs.CONTROL_POINT, ancs.DATA_SOURCE)
        if any(uuid not in characteristics for uuid in needed):
            return self._set_state("unsupported", "the phone's notification service is incomplete")
        for uuid in needed:
            self._handles[uuid] = characteristics[uuid]["value"]

        # Data Source first: once Notification Source is on, the phone sends
        # what is already in its notification centre and the details are
        # requested right away.
        def data_source_on(failure):
            if failure is not None:
                return self._subscription_failed(failure)
            self.att.subscribe(characteristics[ancs.NOTIFICATION_SOURCE], notification_source_on)

        def notification_source_on(failure):
            if failure is not None:
                return self._subscription_failed(failure)
            self._set_state("ready")
            self._start_media()

        self.att.subscribe(characteristics[ancs.DATA_SOURCE], data_source_on)

    def _subscription_failed(self, error):
        if isinstance(error, AttError) and error.needs_permission:
            return self._set_state("needs-permission", "allow notifications for this computer on the iPhone")
        self._fail("could not subscribe: %s" % error)

    def _start_media(self):
        if not self._media_range:
            return
        self.att.discover_characteristics(self._media_range[0], self._media_range[1], self._media_service_found)

    def _media_service_found(self, characteristics, error):
        # Media is a bonus: whatever goes wrong here leaves notifications running.
        if error is not None or ancs.ENTITY_UPDATE not in characteristics:
            return
        entity = characteristics[ancs.ENTITY_UPDATE]
        self._handles[ancs.ENTITY_UPDATE] = entity["value"]

        def subscribed(failure):
            if failure is not None:
                return
            self.media["available"] = True
            for subscription in ancs.MEDIA_SUBSCRIPTIONS:
                self.att.write(entity["value"], subscription)
            self.on_media(dict(self.media))

        self.att.subscribe(entity, subscribed)
        remote = characteristics.get(ancs.REMOTE_COMMAND)
        if remote:
            self._handles[ancs.REMOTE_COMMAND] = remote["value"]
            self.att.subscribe(remote, lambda _failure: None)

    # ---- incoming data ----------------------------------------------------------

    def _on_notify(self, handle, value):
        if handle == self._handles.get(ancs.NOTIFICATION_SOURCE):
            self._event(ancs.parse_event(value))
        elif handle == self._handles.get(ancs.DATA_SOURCE):
            self._data += value
            self._data_arrived()
        elif handle == self._handles.get(ancs.ENTITY_UPDATE):
            update = ancs.parse_media_update(value)
            if update:
                self.media.update(update)
                self.on_media(dict(self.media))
        elif handle == self._handles.get(ancs.REMOTE_COMMAND):
            self.media["commands"] = ancs.parse_supported_commands(value)
            self.on_media(dict(self.media))

    def _event(self, event):
        if event is None:
            return
        uid = event["uid"]
        if event["event"] == "removed":
            self._events.pop(uid, None)
            self._commands = [c for c in self._commands if c[:2] != ("attributes", uid)]
            for held in self._waiting.values():
                held[:] = [n for n in held if n["uid"] != uid]
            self.on_removed(uid)
            return
        self._events[uid] = event
        if ("attributes", uid) not in [c[:2] for c in self._commands] and len(self._commands) < MAX_QUEUED:
            self._commands.append(("attributes", uid, ancs.get_notification_attributes(uid)))
        self._next_command()

    # ---- control point: one command at a time ---------------------------------------

    def _next_command(self):
        if self._current is not None or not self._commands or self.state != "ready":
            return
        kind, key, command = self._commands.pop(0)
        self._current = (kind, key, self._clock() + COMMAND_TIMEOUT)
        self._data = b""

        def written(failure):
            # A notification that is gone by now is refused; an action has no
            # answer on the Data Source at all.
            if self._current and self._current[:2] == (kind, key) and (failure is not None or kind == "action"):
                if kind == "app" and failure is not None:
                    self._app_named(key, "")
                self._finish_command()

        self.att.write(self._handles[ancs.CONTROL_POINT], command, written)

    def _finish_command(self):
        self._current = None
        self._data = b""
        self._next_command()

    def _data_arrived(self):
        if self._current is None:
            self._data = b""
            return
        kind, key, _deadline = self._current
        if kind == "attributes":
            parsed = ancs.parse_notification_attributes(self._data)
            if parsed is None:
                return
            uid, attributes = parsed
            if uid != key:
                self._data = b""   # late answer to a request that was given up on
                return
            self._finish_command()
            self._notification_read(uid, attributes)
        elif kind == "app":
            parsed = ancs.parse_app_attributes(self._data)
            if parsed is None:
                return
            app_id, attributes = parsed
            if app_id != key:
                self._data = b""
                return
            self._finish_command()
            self._app_named(app_id, attributes.get(ancs.APP_ATTR_DISPLAY_NAME, ""))

    def _notification_read(self, uid, attributes):
        event = self._events.get(uid)
        if event is None:
            return   # removed on the phone while its details were on the way
        app_id = attributes.get(ancs.ATTR_APP_ID, "")
        notification = {
            "uid": uid,
            "appId": app_id,
            "app": self._app_names.get(app_id, ""),
            "title": attributes.get(ancs.ATTR_TITLE, ""),
            "subtitle": attributes.get(ancs.ATTR_SUBTITLE, ""),
            "message": attributes.get(ancs.ATTR_MESSAGE, ""),
            "date": ancs.parse_date(attributes.get(ancs.ATTR_DATE, "")),
            "category": event["category"],
            "silent": event["silent"],
            "important": event["important"],
            "preexisting": event["preexisting"],
            "canDismiss": event["canDismiss"],
            "updated": event["event"] == "modified",
        }
        if app_id in self._app_names:
            return self.on_notification(notification)
        first = app_id not in self._waiting
        self._waiting.setdefault(app_id, []).append(notification)
        if first:
            self._commands.insert(0, ("app", app_id, ancs.get_app_attributes(app_id)))
            self._next_command()

    def _app_named(self, app_id, name):
        self._app_names[app_id] = name or fallback_app_name(app_id)
        for notification in self._waiting.pop(app_id, []):
            notification["app"] = self._app_names[app_id]
            self.on_notification(notification)

    # ---- actions ------------------------------------------------------------------

    def dismiss(self, uid):
        """Clear a notification on the phone. False when it cannot be sent now."""
        if self.state != "ready" or uid not in self._events:
            return False
        self._commands.append(("action", uid, ancs.perform_action(uid, ancs.ACTION_NEGATIVE)))
        self._next_command()
        return True

    def media_command(self, name):
        handle = self._handles.get(ancs.REMOTE_COMMAND)
        if self.state != "ready" or handle is None or name not in ancs.MEDIA_COMMANDS:
            return False
        self.att.write(handle, ancs.media_command(name))
        return True
