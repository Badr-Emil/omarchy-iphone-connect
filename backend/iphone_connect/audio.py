"""Call audio through PipeWire.

With PipeWire's native HFP backend in the hands-free role, an active SCO link
shows up as two *streams* (not devices), measured on PipeWire 1.6.8:

  bluez_input.<ADDR>.0   Stream/Output/Audio  phone voice  -> a sink   (PC speakers)
  bluez_output.<ADDR>.1  Stream/Input/Audio   a source     -> phone    (PC microphone)

WirePlumber links them to the default sink/source by itself, so no loopback
is needed. This module only observes those streams, mutes the microphone
stream, and snapshots the default sink/source when a call starts so they can
be restored if anything changed them during the call.
"""

import json
import os
import subprocess

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "iphone-connect")
STATE_FILE = os.path.join(STATE_DIR, "audio.json")
CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "iphone-connect", "config.json")

# WebRTC audio processing built into PipeWire (libspa-aec-webrtc): removes fan
# noise and hum, cancels the echo of the caller's voice from the speakers,
# and levels the microphone.
FILTER_SOURCE = "iphone_call_mic"
FILTER_SINK = "iphone_call_out"
FILTER_ARGS = ("webrtc.noise_suppression=true webrtc.high_pass_filter=true "
               "webrtc.gain_control=true webrtc.extended_filter=true")


def load_config(path=CONFIG_FILE):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_config(config, path=CONFIG_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(config, handle, indent=2)


def noise_suppression_enabled(path=CONFIG_FILE):
    return load_config(path).get("noiseSuppression", True) is not False


class AudioRouter:
    def __init__(self, runner=subprocess.run, state_file=STATE_FILE):
        self.runner = runner
        self.state_file = state_file

    def pactl(self, *args):
        result = self.runner(["pactl", *args], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"pactl {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def pactl_json(self, *args):
        output = self.pactl("-f", "json", *args)
        try:
            return json.loads(output or "[]")
        except ValueError:
            raise RuntimeError(f"pactl {' '.join(args)} returned invalid JSON")

    # ---- state file --------------------------------------------------------------

    def load_state(self):
        try:
            with open(self.state_file) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def save_state(self, state):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, self.state_file)

    def clear_state(self):
        try:
            os.remove(self.state_file)
        except FileNotFoundError:
            pass

    # ---- streams -----------------------------------------------------------------

    @staticmethod
    def _is_phone_stream(entry, prefix, address):
        props = entry.get("properties", {})
        name = str(props.get("node.name", ""))
        if not name.startswith(prefix):
            return False
        if not address:
            return True
        key = address.replace(":", "_").upper()
        return key in name.upper() or str(props.get("api.bluez5.address", "")).upper() == address.upper()

    def streams(self, address=None):
        """Return {"voice": sink-input or None, "mic": source-output or None}."""
        voice = next((e for e in self.pactl_json("list", "sink-inputs")
                      if self._is_phone_stream(e, "bluez_input.", address)), None)
        mic = next((e for e in self.pactl_json("list", "source-outputs")
                    if self._is_phone_stream(e, "bluez_output.", address)), None)
        return {"voice": voice, "mic": mic}

    def _names(self, kind):
        return {str(e.get("index")): e.get("name", "") for e in self.pactl_json("list", "short", kind)}

    # ---- call lifecycle ----------------------------------------------------------------

    def start_call(self, address=None):
        state = self.load_state()
        if not state:
            state = {"default_sink": self.pactl("get-default-sink"),
                     "default_source": self.pactl("get-default-source"),
                     "address": address}
            self.save_state(state)
        return state

    def enhance(self, address=None):
        """Put the WebRTC filter between the PC devices and the call streams."""
        state = self.load_state() or self.start_call(address)
        if state.get("filter_module"):
            return state
        s = self.streams(address)
        if not s["voice"] or not s["mic"]:
            raise RuntimeError("call audio streams not found")
        module = self.pactl("load-module", "module-echo-cancel",
                            f"source_name={FILTER_SOURCE}", f"sink_name={FILTER_SINK}",
                            f"source_master={state['default_source']}", f"sink_master={state['default_sink']}",
                            "aec_method=webrtc", f"aec_args='{FILTER_ARGS}'")
        state["filter_module"] = module
        self.save_state(state)
        try:
            self.pactl("move-sink-input", str(s["voice"]["index"]), FILTER_SINK)
            self.pactl("move-source-output", str(s["mic"]["index"]), FILTER_SOURCE)
        except RuntimeError:
            self.remove_filter()
            raise
        return state

    def remove_filter(self):
        state = self.load_state()
        if state and state.get("filter_module"):
            try:
                self.pactl("unload-module", str(state["filter_module"]))
            except RuntimeError:
                pass
            state.pop("filter_module", None)
            self.save_state(state)

    def end_call(self):
        state = self.load_state()
        if not state:
            return None
        if state.get("filter_module"):
            try:
                self.pactl("unload-module", str(state["filter_module"]))
            except RuntimeError:
                pass
        for kind, key in (("sink", "default_sink"), ("source", "default_source")):
            value = state.get(key)
            if not value or value.startswith("bluez_"):
                continue
            try:
                if self.pactl(f"get-default-{kind}") != value:
                    self.pactl(f"set-default-{kind}", value)
            except RuntimeError:
                pass
        self.clear_state()
        return state

    # ---- controls --------------------------------------------------------------------------

    def set_mute(self, muted, address=None):
        mic = self.streams(address)["mic"]
        if not mic:
            raise RuntimeError("no call audio is active")
        self.pactl("set-source-output-mute", str(mic["index"]), "1" if muted else "0")

    def is_muted(self, address=None):
        mic = self.streams(address)["mic"]
        return bool(mic and mic.get("mute"))

    def describe(self, address=None):
        s = self.streams(address)
        sinks = self._names("sinks")
        sources = self._names("sources")
        voice, mic = s["voice"], s["mic"]
        state = self.load_state() or {}
        output = sinks.get(str(voice.get("sink"))) if voice else self.pactl("get-default-sink")
        microphone = sources.get(str(mic.get("source"))) if mic else self.pactl("get-default-source")
        filtered = microphone == FILTER_SOURCE
        # Behind the filter, report the real PC devices it is attached to.
        if output == FILTER_SINK:
            output = state.get("default_sink", output)
        if filtered:
            microphone = state.get("default_source", microphone)
        return {
            "routed": bool(voice and mic),
            "noiseSuppression": filtered,
            "noiseSuppressionEnabled": noise_suppression_enabled(),
            "output": output,
            "microphone": microphone,
            "muted": bool(mic and mic.get("mute")),
            "rate": (voice or {}).get("sample_specification", ""),
        }
