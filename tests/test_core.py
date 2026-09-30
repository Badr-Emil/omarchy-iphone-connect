import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from iphone_connect import audio, events, phone  # noqa: E402
from iphone_connect.state import CallState, CallStateMachine, InvalidTransition, from_pipewire  # noqa: E402


class PhoneNumberTests(unittest.TestCase):
    def test_normalizes_separators(self):
        self.assertEqual(phone.normalize("+43 (660) 123-45.67"), "+436601234567")

    def test_accepts_short_and_service_codes(self):
        self.assertEqual(phone.normalize("112"), "112")
        self.assertEqual(phone.normalize("*100#"), "*100#")

    def test_rejects_letters_and_bad_plus(self):
        for bad in ("abc", "+43+660", "0660x12", "", None, "1"):
            with self.assertRaises(phone.InvalidNumber, msg=bad):
                phone.normalize(bad)

    def test_rejects_too_long(self):
        with self.assertRaises(phone.InvalidNumber):
            phone.normalize("1" * 21)

    def test_mask_keeps_prefix_and_end(self):
        self.assertEqual(phone.mask("+436601234567"), "+436*******67")
        self.assertEqual(phone.mask("112"), "***")
        self.assertEqual(phone.mask(""), "")

    def test_dtmf(self):
        self.assertTrue(phone.valid_dtmf("123*#A"))
        self.assertFalse(phone.valid_dtmf("12x"))
        self.assertFalse(phone.valid_dtmf(""))


class StateMachineTests(unittest.TestCase):
    def test_outgoing_flow(self):
        m = CallStateMachine()
        for s in (CallState.DIALING, CallState.ALERTING, CallState.ACTIVE, CallState.DISCONNECTED, CallState.IDLE):
            self.assertTrue(m.transition(s))
        self.assertEqual(m.history[-1], CallState.IDLE)

    def test_incoming_answer_and_reject(self):
        m = CallStateMachine()
        m.transition(CallState.INCOMING)
        self.assertTrue(m.in_call)
        self.assertFalse(m.needs_audio)
        m.transition(CallState.ACTIVE)
        self.assertTrue(m.needs_audio)

        r = CallStateMachine()
        r.transition(CallState.INCOMING)
        r.transition(CallState.DISCONNECTED)
        self.assertFalse(r.in_call)

    def test_invalid_transitions(self):
        m = CallStateMachine()
        with self.assertRaises(InvalidTransition):
            m.transition(CallState.DISCONNECTED)
        m.transition(CallState.INCOMING)
        with self.assertRaises(InvalidTransition):
            m.transition(CallState.DIALING)

    def test_same_state_is_noop(self):
        m = CallStateMachine()
        m.transition(CallState.DIALING)
        self.assertFalse(m.transition(CallState.DIALING))

    def test_adopts_running_call(self):
        m = CallStateMachine()
        self.assertTrue(m.transition(CallState.ACTIVE))

    def test_hold(self):
        m = CallStateMachine()
        m.transition(CallState.ACTIVE)
        m.transition(CallState.HELD)
        m.transition(CallState.ACTIVE)
        self.assertEqual(m.state, CallState.ACTIVE)

    def test_pipewire_mapping(self):
        self.assertEqual(from_pipewire("waiting"), CallState.INCOMING)
        self.assertEqual(from_pipewire("ALERTING"), CallState.ALERTING)
        with self.assertRaises(ValueError):
            from_pipewire("ringing-ish")


class EventMappingTests(unittest.TestCase):
    AG = "/org/pipewire/Telephony/ag0"
    CALL = "/org/pipewire/Telephony/ag0/call1"

    def test_phone_connected(self):
        evs = events.interfaces_added(self.AG, {events.AG_IFACE: {"Address": "AA:BB"}})
        self.assertEqual(evs, [{"type": "phone-connected", "path": self.AG, "address": "AA:BB"}])

    def test_incoming_call_with_caller_id(self):
        evs = events.interfaces_added(self.CALL, {events.CALL_IFACE: {"State": "incoming", "LineIdentification": "+43660"}})
        self.assertEqual(evs[0]["type"], "call-added")
        self.assertEqual(evs[0]["state"], "incoming")
        self.assertEqual(evs[0]["number"], "+43660")

    def test_state_and_caller_id_changes(self):
        evs = events.properties_changed(self.CALL, events.CALL_IFACE, {"State": "active", "LineIdentification": "123"})
        self.assertEqual([e["type"] for e in evs], ["call-state", "caller-id"])

    def test_removed(self):
        self.assertEqual(events.interfaces_removed(self.CALL, [events.CALL_IFACE])[0]["type"], "call-removed")
        self.assertEqual(events.interfaces_removed(self.AG, [events.AG_IFACE])[0]["type"], "phone-disconnected")

    def test_transport_codec(self):
        ev = events.properties_changed(self.AG, events.TRANSPORT_IFACE, {"State": "active", "Codec": 2})[0]
        self.assertEqual(ev["codec"], "mSBC")
        self.assertTrue(ev["wideband"])
        ev = events.properties_changed(self.AG, events.TRANSPORT_IFACE, {"Codec": 1})[0]
        self.assertFalse(ev["wideband"])

    def test_unrelated_interface_ignored(self):
        self.assertEqual(events.properties_changed(self.AG, "org.example.Other", {"X": 1}), [])

    def test_ag_path_of(self):
        self.assertEqual(events.ag_path_of(self.CALL), self.AG)


class FakePactl:
    """Simulates the pactl commands AudioRouter uses (streams as measured on PipeWire 1.6.8)."""

    def __init__(self, fail=(), in_call=True):
        self.default_sink = "alsa_output.speakers"
        self.default_source = "alsa_input.mic"
        self.fail = set(fail)
        self.in_call = in_call
        self.mic_mute = False
        self.calls = []
        self.modules = {}
        self.voice_sink = 59
        self.mic_source = 60

    def _json(self, what):
        filt = bool(self.modules)
        if what == ["list", "short", "sinks"]:
            return [{"index": 59, "name": "alsa_output.speakers"}] + ([{"index": 70, "name": "iphone_call_out"}] if filt else [])
        if what == ["list", "short", "sources"]:
            return [{"index": 60, "name": "alsa_input.mic"}] + ([{"index": 71, "name": "iphone_call_mic"}] if filt else [])
        if not self.in_call:
            return []
        if what == ["list", "sink-inputs"]:
            return [{"index": 7, "sink": self.voice_sink, "mute": False, "sample_specification": "float32le 1ch 24000Hz",
                     "properties": {"node.name": "bluez_input.AA_BB_CC_DD_EE_FF.0"}}]
        if what == ["list", "source-outputs"]:
            return [{"index": 9, "source": 60, "mute": False, "properties": {"node.name": "quickshell"}},
                    {"index": 8, "source": self.mic_source, "mute": self.mic_mute,
                     "properties": {"node.name": "bluez_output.AA_BB_CC_DD_EE_FF.1"}}]
        return []

    def __call__(self, argv, **_kwargs):
        args = argv[1:]
        self.calls.append(args)
        if args and args[0] in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        out = ""
        if args[:2] == ["-f", "json"]:
            out = json.dumps(self._json(args[2:]))
        elif args[0] == "get-default-sink":
            out = self.default_sink
        elif args[0] == "get-default-source":
            out = self.default_source
        elif args[0] == "set-default-sink":
            self.default_sink = args[1]
        elif args[0] == "set-default-source":
            self.default_source = args[1]
        elif args[0] == "load-module":
            idx = str(500 + len(self.modules))
            self.modules[idx] = args[1:]
            out = idx
        elif args[0] == "unload-module":
            self.modules.pop(args[1], None)
            self.voice_sink, self.mic_source = 59, 60  # streams fall back to defaults
        elif args[0] == "move-sink-input":
            self.voice_sink = {"iphone_call_out": 70}.get(args[2], 59)
        elif args[0] == "move-source-output":
            self.mic_source = {"iphone_call_mic": 71}.get(args[2], 60)
        elif args[0] == "set-source-output-mute":
            assert args[1] == "8", "must mute the phone stream only"
            self.mic_mute = args[2] == "1"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class AudioRestoreTests(unittest.TestCase):
    ADDR = "AA:BB:CC:DD:EE:FF"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "audio.json")
        self.pactl = FakePactl()
        self.router = audio.AudioRouter(runner=self.pactl, state_file=self.state_file)

    def tearDown(self):
        self.tmp.cleanup()

    def test_describe_active_call(self):
        info = self.router.describe(self.ADDR)
        self.assertTrue(info["routed"])
        self.assertEqual(info["output"], "alsa_output.speakers")
        self.assertEqual(info["microphone"], "alsa_input.mic")

    def test_describe_without_call(self):
        router = audio.AudioRouter(runner=FakePactl(in_call=False), state_file=self.state_file)
        info = router.describe(self.ADDR)
        self.assertFalse(info["routed"])
        self.assertEqual(info["output"], "alsa_output.speakers")

    def test_restores_changed_defaults(self):
        self.router.start_call(self.ADDR)
        self.pactl.default_sink = "alsa_output.hdmi"
        self.pactl.default_source = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        self.router.end_call()
        self.assertEqual(self.pactl.default_sink, "alsa_output.speakers")
        self.assertEqual(self.pactl.default_source, "alsa_input.mic")
        self.assertFalse(os.path.exists(self.state_file))

    def test_unchanged_defaults_are_not_touched(self):
        self.router.start_call(self.ADDR)
        self.router.end_call()
        self.assertFalse(any(c[0].startswith("set-default") for c in self.pactl.calls))

    def test_snapshot_survives_restart(self):
        self.router.start_call(self.ADDR)
        self.pactl.default_sink = "alsa_output.hdmi"
        fresh = audio.AudioRouter(runner=self.pactl, state_file=self.state_file)
        self.assertIsNotNone(fresh.end_call())
        self.assertEqual(self.pactl.default_sink, "alsa_output.speakers")

    def test_start_is_idempotent(self):
        self.router.start_call(self.ADDR)
        self.pactl.default_sink = "alsa_output.hdmi"
        self.router.start_call(self.ADDR)
        self.assertEqual(self.router.load_state()["default_sink"], "alsa_output.speakers")

    def test_mute_only_phone_stream(self):
        self.router.set_mute(True, self.ADDR)
        self.assertTrue(self.router.is_muted(self.ADDR))
        self.router.set_mute(False, self.ADDR)
        self.assertFalse(self.router.is_muted(self.ADDR))

    def test_mute_without_call_fails(self):
        router = audio.AudioRouter(runner=FakePactl(in_call=False), state_file=self.state_file)
        with self.assertRaises(RuntimeError):
            router.set_mute(True, self.ADDR)

    def test_end_without_call_is_noop(self):
        self.assertIsNone(self.router.end_call())

    def test_pactl_error(self):
        router = audio.AudioRouter(runner=FakePactl(fail={"get-default-sink"}), state_file=self.state_file)
        with self.assertRaises(RuntimeError):
            router.start_call(self.ADDR)


class NoiseSuppressionTests(unittest.TestCase):
    ADDR = "AA:BB:CC:DD:EE:FF"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pactl = FakePactl()
        self.router = audio.AudioRouter(runner=self.pactl, state_file=os.path.join(self.tmp.name, "a.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_filter_is_inserted_and_removed(self):
        self.router.start_call(self.ADDR)
        self.router.enhance(self.ADDR)
        self.assertEqual(len(self.pactl.modules), 1)
        args = list(self.pactl.modules.values())[0]
        self.assertIn("aec_method=webrtc", args)
        self.assertIn("source_master=alsa_input.mic", args)
        info = self.router.describe(self.ADDR)
        self.assertTrue(info["noiseSuppression"])
        # the real devices are reported, not the filter nodes
        self.assertEqual(info["microphone"], "alsa_input.mic")
        self.assertEqual(info["output"], "alsa_output.speakers")
        self.router.end_call()
        self.assertEqual(self.pactl.modules, {})
        self.assertFalse(self.router.describe(self.ADDR)["noiseSuppression"])

    def test_enhance_is_idempotent(self):
        self.router.enhance(self.ADDR)
        self.router.enhance(self.ADDR)
        self.assertEqual(len(self.pactl.modules), 1)

    def test_enhance_without_call_fails_cleanly(self):
        router = audio.AudioRouter(runner=FakePactl(in_call=False), state_file=os.path.join(self.tmp.name, "b.json"))
        with self.assertRaises(RuntimeError):
            router.enhance(self.ADDR)

    def test_config_default_on(self):
        path = os.path.join(self.tmp.name, "cfg.json")
        self.assertTrue(audio.noise_suppression_enabled(path))
        audio.save_config({"noiseSuppression": False}, path)
        self.assertFalse(audio.noise_suppression_enabled(path))


class ContactsTests(unittest.TestCase):
    VCARDS = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Mustermann;Max;;;\r\nFN:Max Mustermann\r\n"
        "TEL;TYPE=CELL:+43 660 1234567\r\nTEL;TYPE=HOME:01 234 5678\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Muster;Anna;;;\r\nTEL:0664 999\r\n 8877\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No Number\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Notruf\r\nTEL:112\r\nEND:VCARD\r\n"
    )

    def setUp(self):
        from iphone_connect import contacts
        self.contacts = contacts

    def test_parse(self):
        parsed = self.contacts.parse_vcards(self.VCARDS)
        self.assertEqual([c["name"] for c in parsed], ["Max Mustermann", "Anna Muster", "Notruf"])
        self.assertEqual(parsed[1]["numbers"], ["0664 9998877"])  # folded line joined

    def test_lookup_matches_national_and_international(self):
        index = self.contacts.build_index(self.contacts.parse_vcards(self.VCARDS))
        self.assertEqual(self.contacts.lookup("+436601234567", index), "Max Mustermann")
        self.assertEqual(self.contacts.lookup("06601234567", index), "Max Mustermann")
        self.assertEqual(self.contacts.lookup("+436649998877", index), "Anna Muster")
        self.assertEqual(self.contacts.lookup("112", index), "Notruf")
        self.assertEqual(self.contacts.lookup("+491701234599", index), "")
        self.assertEqual(self.contacts.lookup("", index), "")

    def test_cache_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "contacts.json")
            self.contacts.save_cache(self.contacts.parse_vcards(self.VCARDS), path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(self.contacts.load_index(path)[self.contacts.match_key("+436601234567")], "Max Mustermann")
            names = [c["name"] for c in self.contacts.load_contacts(path)]
            self.assertEqual(names, ["Anna Muster", "Max Mustermann", "Notruf"])


class CliParserTests(unittest.TestCase):
    def test_every_command_parses(self):
        from iphone_connect.cli import build_parser
        parser = build_parser()
        for argv in (["status", "--json"], ["devices"], ["pair"], ["connect"], ["disconnect"],
                     ["call", "+43660"], ["answer"], ["reject"], ["hangup"], ["redial"], ["take"],
                     ["tones", "1"], ["mute", "on"], ["noise", "off"], ["notifications", "off"], ["ringtone", "on"],
                     ["contacts", "list", "--json"], ["history", "sync"], ["history", "--json"], ["history", "seen"], ["volume", "50"], ["watch"], ["daemon"], ["diagnostics"]):
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func), argv)


class HistoryTests(unittest.TestCase):
    TEXT = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:\r\nN:;;;;\r\nTEL:+436601234567\r\n"
        "X-IRMC-CALL-DATETIME;MISSED:20260930T195248\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Max Mustermann\r\nN:Mustermann;Max;;;\r\nTEL;TYPE=CELL:0664 1\r\n"
        "X-IRMC-CALL-DATETIME;RECEIVED:20260930T130849\r\nEND:VCARD\r\n"
        "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna\r\nTEL:0676 2\r\n"
        "X-IRMC-CALL-DATETIME;DIALED:20260930T210000\r\nEND:VCARD\r\n"
    )

    def setUp(self):
        from iphone_connect import contacts
        self.c = contacts

    def test_parse_types_times_newest_first(self):
        entries = self.c.parse_history(self.TEXT)
        self.assertEqual([e["type"] for e in entries], ["dialed", "missed", "received"])
        self.assertEqual(entries[1]["time"], "2026-09-30T19:52:48")
        self.assertEqual(entries[1]["name"], "")
        self.assertEqual(entries[2]["name"], "Max Mustermann")

    def test_unseen_missed(self):
        entries = self.c.parse_history(self.TEXT)
        self.assertEqual(self.c.unseen_missed({"entries": entries, "seen": ""}), 1)
        self.assertEqual(self.c.unseen_missed({"entries": entries, "seen": "2026-09-30T21:00:00"}), 0)

    def test_merge_adds_calls_missing_from_the_phone(self):
        pbap = {"entries": self.c.parse_history(self.TEXT), "seen": ""}
        live = [
            {"type": "missed", "time": "2026-09-30T22:04:52", "number": "+43 664 5550101", "name": "Lena"},
            # same call the phone already reported (within 2 minutes) -> not duplicated
            {"type": "missed", "time": "2026-09-30T19:53:30", "number": "0660 1234567", "name": ""},
        ]
        merged = self.c.merged_history(pbap, live)
        self.assertEqual(len(merged["entries"]), 4)
        self.assertEqual(merged["entries"][0]["time"], "2026-09-30T22:04:52")
        self.assertEqual(self.c.unseen_missed(merged), 2)

    def test_record_call_is_private_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.json")
            for i in range(self.c.CALLLOG_MAX + 5):
                self.c.record_call("dialed", "123", "", f"2026-09-30T10:00:{i % 60:02d}", path)
            self.assertEqual(len(self.c.load_calllog(path)), self.c.CALLLOG_MAX)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_mark_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            self.c._write_private(path, {"entries": self.c.parse_history(self.TEXT), "seen": ""})
            self.c.mark_seen(path)
            self.assertEqual(self.c.unseen_missed(self.c.load_history(path)), 0)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class DeviceStateTests(unittest.TestCase):
    def test_is_phone(self):
        from iphone_connect import bluez
        self.assertTrue(bluez.is_phone({"UUIDs": [bluez.HFP_AG_UUID.upper()]}))
        self.assertTrue(bluez.is_phone({"Icon": "phone"}))
        self.assertFalse(bluez.is_phone({"Icon": "audio-headphones", "UUIDs": []}))



class TelephonyObjectsTests(unittest.TestCase):
    def test_calls_are_read_from_each_phone(self):
        from iphone_connect.telephony import Telephony
        tel = Telephony.__new__(Telephony)
        ag = "/org/pipewire/Telephony/ag1"
        tree = {
            "/org/pipewire/Telephony": {ag: {events.AG_IFACE: {"Address": "AA"}}},
            ag: {ag + "/call1": {events.CALL_IFACE: {"State": "active", "LineIdentification": "+43660"}}},
        }
        tel._managed = lambda path: tree[path]
        calls = tel.calls(ag)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["state"], "active")


if __name__ == "__main__":
    unittest.main()
