import errno
import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(__file__))

from iphone_connect import ancs, notifications  # noqa: E402
from sim_phone import SimPhone  # noqa: E402

try:
    from gi.repository import GLib
    from iphone_connect import mirror as mirror_module
    from iphone_connect.mirror import Mirror
except ImportError:      # PyGObject lives in the system Python only
    GLib = None


def notification(uid, **changes):
    base = {"uid": uid, "appId": "com.apple.MobileSMS", "app": "Messages", "title": "Mum", "subtitle": "",
            "message": "Call me back", "date": "2026-10-02T09:41:18", "category": "social", "silent": False,
            "important": False, "preexisting": False, "canDismiss": True, "updated": False}
    base.update(changes)
    return base


class StoreTests(unittest.TestCase):
    def test_newest_first_and_replace(self):
        store = notifications.Store()
        self.assertTrue(store.apply(notification(1, date="2026-10-02T08:00:00")))
        self.assertTrue(store.apply(notification(2, date="2026-10-02T09:00:00")))
        self.assertTrue(store.apply(notification(3, date="")))
        self.assertFalse(store.apply(notification(1, date="2026-10-02T08:00:00", message="edited")))
        self.assertEqual([n["uid"] for n in store.listing()], [2, 1, 3])
        self.assertEqual(store.listing()[1]["message"], "edited")
        self.assertNotIn("preexisting", store.listing()[0])
        self.assertTrue(store.remove(2))
        self.assertFalse(store.remove(2))

    def test_listing_is_capped(self):
        store = notifications.Store()
        for uid in range(notifications.LIMIT + 15):
            store.apply(notification(uid))
        listing = store.listing()
        self.assertEqual(len(listing), notifications.LIMIT)
        self.assertEqual(listing[0]["uid"], notifications.LIMIT + 14)


class ToastRuleTests(unittest.TestCase):
    def test_off_until_the_user_turns_it_on(self):
        self.assertFalse(notifications.mirror_enabled({}))
        self.assertFalse(notifications.mirror_enabled({"mirror": "yes"}))
        self.assertTrue(notifications.mirror_enabled({"mirror": True}))

    def test_which_notifications_pop_up(self):
        self.assertTrue(notifications.should_toast(notification(1), {}))
        self.assertFalse(notifications.should_toast(notification(1, preexisting=True), {}))
        self.assertFalse(notifications.should_toast(notification(1, silent=True), {}))
        self.assertFalse(notifications.should_toast(notification(1, category="incoming-call"), {}))
        self.assertTrue(notifications.should_toast(notification(1, category="missed-call"), {}))
        self.assertFalse(notifications.should_toast(notification(1), {"mirrorToasts": False}))
        self.assertFalse(notifications.should_toast(notification(1), {"mirrorMutedApps": ["com.apple.MobileSMS"]}))

    def test_toast_text(self):
        self.assertEqual(notifications.toast_text(notification(1, subtitle="Family")),
                         ("Messages", "Mum", "Family\nCall me back"))
        self.assertEqual(notifications.toast_text(notification(1, app="", title="", message="")), ("iPhone", "iPhone", ""))


class StateFileTests(unittest.TestCase):
    def test_round_trip_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "run", "mirror.json")
            store = notifications.Store()
            store.apply(notification(1))
            notifications.write_state(notifications.snapshot(True, "ready", "", store, {"available": True}), path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            data = notifications.read_state(path)
            self.assertEqual((data["state"], data["notifications"][0]["title"], data["media"]["available"]),
                             ("ready", "Mum", True))
            notifications.remove_state(path)
            self.assertEqual(notifications.read_state(path), notifications.snapshot(False))

    def test_nothing_leaks_while_off(self):
        store = notifications.Store()
        store.apply(notification(1))
        data = notifications.snapshot(False, "ready", "x", store, {"available": True, "title": "Song"})
        self.assertEqual((data["state"], data["detail"], data["notifications"], data["media"]["title"]),
                         ("off", "", [], ""))

    def test_broken_file_reads_as_off(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "mirror.json")
            for content in ("", "{", "[]", '{"notifications": 5}'):
                with open(path, "w") as handle:
                    handle.write(content)
                self.assertEqual(notifications.read_state(path)["state"], "off")


class FakeToaster:
    def __init__(self):
        self.shown, self.closed, self.forgotten = [], [], 0

    def show(self, n):
        self.shown.append(n["uid"])

    def close(self, uid):
        self.closed.append(uid)

    def forget(self):
        self.forgotten += 1


@unittest.skipIf(GLib is None, "needs PyGObject (run with /usr/bin/python3)")
class MirrorTests(unittest.TestCase):
    """The real Mirror over a real socket, with the simulated iPhone at the other end."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = os.path.join(self.directory.name, "mirror.json")
        self.config = {"mirror": True}
        self.phone = SimPhone()
        self.toaster = FakeToaster()
        self.logs = []
        self.opened = []
        self.phone_sock = None
        self.phone_watch = 0
        self.refuse = None
        self.mirror = Mirror(self.toaster, log=self.logs.append, opener=self.opener,
                             config_loader=lambda: dict(self.config), state_path=self.state_path)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.mirror.stop()
        self.hang_up()

    def opener(self, address):
        if self.refuse:
            raise OSError(self.refuse, os.strerror(self.refuse))
        self.opened.append(address)
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ours.setblocking(False)
        theirs.setblocking(False)
        self.phone_sock = theirs
        self.phone_watch = GLib.io_add_watch(theirs.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN, self.phone_readable)
        return ours

    def phone_readable(self, _fd, _condition):
        try:
            packet = self.phone_sock.recv(4096)
        except (BlockingIOError, OSError):
            return True
        if not packet:            # the computer closed the channel
            self.phone_watch = 0
            return False
        self.phone.receive(packet)
        self.flush()
        return True

    def flush(self):
        while self.phone.outbox and self.phone_sock:
            try:
                self.phone_sock.send(self.phone.outbox.popleft())
            except OSError:
                return

    def hang_up(self):
        if self.phone_watch:
            GLib.source_remove(self.phone_watch)
            self.phone_watch = 0
        if self.phone_sock:
            self.phone_sock.close()
            self.phone_sock = None

    def wait(self, condition, seconds=3):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.flush()
            while context.iteration(False):
                pass
            if condition():
                return True
            time.sleep(0.005)
        return False

    def state_file(self):
        with open(self.state_path) as handle:
            return json.load(handle)

    def ready(self):
        self.mirror.phone_connected("AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.wait(lambda: self.mirror.state == "ready"), self.mirror.state)

    def test_mirrors_and_publishes(self):
        self.phone.notifications[5] = {"app": "com.apple.MobileSMS", "title": "Old", "subtitle": "", "message": "",
                                       "date": "20261001T100000", "category": 4, "flags": 0}
        self.ready()
        self.assertEqual(self.opened, ["AA:BB:CC:DD:EE:FF"])
        self.phone.add(6, title="New")
        self.assertTrue(self.wait(lambda: len(self.mirror.store.items) == 2))
        self.assertEqual(self.toaster.shown, [6], "what was already on the phone does not pop up again")
        self.assertTrue(self.wait(lambda: os.path.exists(self.state_path)
                                  and len(self.state_file()["notifications"]) == 2))
        data = self.state_file()
        self.assertEqual((data["enabled"], data["state"]), (True, "ready"))
        self.assertEqual([n["title"] for n in data["notifications"]], ["New", "Old"])
        self.assertEqual(data["media"]["title"], "Sinnerman")
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)
        self.assertNotIn("New", " ".join(self.logs), "notification texts never reach the log")

    def test_removal_and_dismiss(self):
        self.ready()
        self.phone.add(1)
        self.phone.add(2)
        self.assertTrue(self.wait(lambda: len(self.mirror.store.items) == 2))
        self.phone.remove(1)
        self.assertTrue(self.wait(lambda: 1 in self.toaster.closed))
        self.assertTrue(self.mirror.dismiss(2))
        self.assertTrue(self.wait(lambda: self.phone.dismissed == [2] and not self.mirror.store.items))
        self.assertFalse(self.mirror.dismiss(2))
        self.assertTrue(self.mirror.media("toggle"))
        self.assertTrue(self.wait(lambda: self.phone.media_commands == [2]))

    def test_muted_apps_are_listed_but_quiet(self):
        self.config["mirrorMutedApps"] = ["net.whatsapp.WhatsApp"]
        self.ready()
        self.phone.add(1, app="net.whatsapp.WhatsApp")
        self.phone.add(2)
        self.assertTrue(self.wait(lambda: len(self.mirror.store.items) == 2))
        self.assertEqual(self.toaster.shown, [2])

    def test_stays_off_until_enabled_and_turns_off_again(self):
        self.config = {}
        self.mirror.phone_connected("AA:BB:CC:DD:EE:FF")
        self.assertFalse(self.wait(lambda: self.opened, seconds=0.3), "no channel without the user's consent")
        self.assertEqual(self.mirror.state, "off")

        self.config = {"mirror": True}
        self.mirror.refresh()
        self.assertTrue(self.wait(lambda: self.mirror.state == "ready"))
        self.phone.add(1)
        self.assertTrue(self.wait(lambda: self.mirror.store.items))

        self.config = {"mirror": False}
        self.mirror.refresh()
        self.assertTrue(self.wait(lambda: os.path.exists(self.state_path) and self.state_file()["state"] == "off"))
        self.assertEqual(self.state_file()["notifications"], [])
        self.assertIsNone(self.mirror.sock)
        self.assertTrue(self.wait(lambda: self.phone_sock.recv(10) == b"" if self._peer_closed() else False, seconds=1))

    def _peer_closed(self):
        try:
            return self.phone_sock.recv(10, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False

    def test_waits_for_the_phone_and_drops_everything_when_it_leaves(self):
        self.mirror.refresh()
        self.assertEqual((self.mirror.state, self.mirror.detail), ("waiting", "the iPhone is not connected"))
        self.ready()
        self.phone.add(1)
        self.assertTrue(self.wait(lambda: self.mirror.store.items))
        self.mirror.phone_disconnected()
        self.assertEqual(self.mirror.state, "waiting")
        self.assertEqual(self.mirror.store.items, {})
        self.assertEqual(self.mirror._retry_timer, 0, "nothing to retry without a phone")

    def test_a_closed_channel_is_retried(self):
        self.ready()
        self.hang_up()
        self.assertTrue(self.wait(lambda: self.mirror.state == "waiting"))
        self.assertEqual(self.mirror.detail, "the iPhone closed the data channel")
        self.assertNotEqual(self.mirror._retry_timer, 0)
        self.assertIn("next try in 5 s", self.logs[-1])

    def test_a_refused_channel_tells_the_user_what_to_do(self):
        self.refuse = errno.ECONNREFUSED
        self.mirror.phone_connected("AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.mirror.state, "waiting")
        self.assertIn("switch Bluetooth off and on", self.mirror.detail)
        self.assertIn("next try in %d s" % mirror_module.REFUSED_RETRY_SECONDS, self.logs[-1])

    def test_missing_permission_is_reported_and_asked_again(self):
        self.phone.authorized = False
        self.mirror.phone_connected("AA:BB:CC:DD:EE:FF")
        self.assertTrue(self.wait(lambda: self.mirror.state == "needs-permission"))
        self.phone.authorized = True          # the user tapped "allow" on the phone
        self.mirror.session.retry()
        self.assertTrue(self.wait(lambda: self.mirror.state == "ready"))


@unittest.skipIf(GLib is None, "needs PyGObject (run with /usr/bin/python3)")
class ControlTests(unittest.TestCase):
    """The CLI reaches the service over D-Bus. Runs on a private bus so it can
    never talk to the real service (and through it to a real phone)."""

    @classmethod
    def setUpClass(cls):
        from gi.repository import Gio
        cls.Gio = Gio
        cls.private_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.private_bus.up()

    @classmethod
    def tearDownClass(cls):
        cls.private_bus.down()

    def test_cli_commands_arrive_at_the_mirror(self):
        import subprocess
        from iphone_connect.control import Control

        class FakeMirror:
            def __init__(self):
                self.calls = []
                self.store = notifications.Store()
                self.store.apply(notification(4))
                self.store.apply(notification(5, canDismiss=False))

            def dismiss(self, uid):
                self.calls.append(("dismiss", uid))
                return uid == 4

            def media(self, command):
                self.calls.append(("media", command))
                return command == "next"

        Gio = self.Gio
        bus = Gio.DBusConnection.new_for_address_sync(
            self.private_bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        fake = FakeMirror()
        control = Control(bus, fake)
        control.register()
        self.addCleanup(control.unregister)
        cli = os.path.join(os.path.dirname(__file__), "..", "backend", "iphone-connect")
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.private_bus.get_bus_address(),
                   XDG_CONFIG_HOME=tempfile.mkdtemp(), XDG_RUNTIME_DIR=tempfile.mkdtemp())

        def run(*args):
            process = subprocess.Popen([cli, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.005)
            out, err = process.communicate(timeout=5)
            return process.returncode, out + err

        self.assertEqual(run("media", "next")[0], 0)
        code, text = run("media", "pause")
        self.assertEqual(code, 1)
        self.assertIn("not connected for media control", text)
        self.assertEqual(run("mirror", "dismiss", "4")[0], 0)
        self.assertEqual(run("mirror", "dismiss", "9")[0], 1)
        code, text = run("mirror", "dismiss", "all")
        self.assertIn("clear 1 notifications", text)
        self.assertEqual(fake.calls, [("media", "next"), ("media", "pause"), ("dismiss", 4), ("dismiss", 9),
                                      ("dismiss", 4)])

    def test_cli_says_when_the_service_is_not_running(self):
        import subprocess
        cli = os.path.join(os.path.dirname(__file__), "..", "backend", "iphone-connect")
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.private_bus.get_bus_address())
        result = subprocess.run([cli, "media", "play"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("background service is not running", result.stderr)


if __name__ == "__main__":
    unittest.main()
