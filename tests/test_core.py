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
    """Simulates the pactl commands AudioRouter uses."""

    def __init__(self, fail=()):
        self.default_sink = "alsa_output.speakers"
        self.default_source = "alsa_input.mic"
        self.modules = {}
        self.next_module = 500
        self.mute = {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, argv, **_kwargs):
        args = argv[1:]
        self.calls.append(args)
        out, code = "", 0
        if args[0] in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if args[0] == "get-default-sink":
            out = self.default_sink
        elif args[0] == "get-default-source":
            out = self.default_source
        elif args[0] == "set-default-sink":
            self.default_sink = args[1]
        elif args[0] == "set-default-source":
            self.default_source = args[1]
        elif args[:3] == ["list", "short", "sources"]:
            out = "1\talsa_input.mic\n2\tbluez_input.AA_BB_CC_DD_EE_FF.0\n3\tbluez_output.AA_BB_CC_DD_EE_FF.1.monitor"
        elif args[:3] == ["list", "short", "sinks"]:
            out = "1\talsa_output.speakers\n3\tbluez_output.AA_BB_CC_DD_EE_FF.1"
        elif args[0] == "load-module":
            self.next_module += 1
            self.modules[str(self.next_module)] = args[1:]
            out = str(self.next_module)
        elif args[0] == "unload-module":
            self.modules.pop(args[1], None)
        elif args[0] == "set-sink-mute":
            self.mute[args[1]] = args[2] == "1"
        elif args[0] == "get-sink-mute":
            out = "Mute: yes" if self.mute.get(args[1]) else "Mute: no"
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


class AudioRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "audio.json")
        self.pactl = FakePactl()
        self.router = audio.AudioRouter(runner=self.pactl, state_file=self.state_file)

    def tearDown(self):
        self.tmp.cleanup()

    def test_routes_and_restores(self):
        state = self.router.start_call("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(state["modules"]), 2)
        self.assertEqual(state["phone_source"], "bluez_input.AA_BB_CC_DD_EE_FF.0")
        self.assertEqual(state["phone_sink"], "bluez_output.AA_BB_CC_DD_EE_FF.1")
        self.assertEqual(len(self.pactl.modules), 2)

        # something changed the defaults during the call
        self.pactl.default_sink = "bluez_output.AA_BB_CC_DD_EE_FF.1"
        self.router.end_call()
        self.assertEqual(self.pactl.modules, {})
        self.assertEqual(self.pactl.default_sink, "alsa_output.speakers")
        self.assertEqual(self.pactl.default_source, "alsa_input.mic")
        self.assertFalse(os.path.exists(self.state_file))

    def test_start_is_idempotent(self):
        self.router.start_call("AA:BB:CC:DD:EE:FF")
        self.router.start_call("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(self.pactl.modules), 2)

    def test_restore_survives_restart(self):
        self.router.start_call("AA:BB:CC:DD:EE:FF")
        fresh = audio.AudioRouter(runner=self.pactl, state_file=self.state_file)
        self.assertIsNotNone(fresh.end_call())
        self.assertEqual(self.pactl.modules, {})

    def test_missing_phone_nodes_is_reported(self):
        state = self.router.start_call("11:22:33:44:55:66")
        self.assertEqual(state["error"], "phone audio nodes not found")
        self.assertEqual(self.pactl.modules, {})
        with open(self.state_file) as handle:
            self.assertEqual(json.load(handle)["default_sink"], "alsa_output.speakers")

    def test_mute(self):
        with self.assertRaises(RuntimeError):
            self.router.set_mute(True)
        self.router.start_call("AA:BB:CC:DD:EE:FF")
        self.router.set_mute(True)
        self.assertTrue(self.router.is_muted())

    def test_end_without_call_is_noop(self):
        self.assertIsNone(self.router.end_call())

    def test_pactl_error(self):
        router = audio.AudioRouter(runner=FakePactl(fail={"get-default-sink"}), state_file=self.state_file)
        with self.assertRaises(RuntimeError):
            router.start_call("AA:BB:CC:DD:EE:FF")


class DeviceStateTests(unittest.TestCase):
    def test_is_phone(self):
        from iphone_connect import bluez
        self.assertTrue(bluez.is_phone({"UUIDs": [bluez.HFP_AG_UUID.upper()]}))
        self.assertTrue(bluez.is_phone({"Icon": "phone"}))
        self.assertFalse(bluez.is_phone({"Icon": "audio-headphones", "UUIDs": []}))


if __name__ == "__main__":
    unittest.main()
