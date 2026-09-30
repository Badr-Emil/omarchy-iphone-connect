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

    def end_call(self):
        state = self.load_state()
        if not state:
            return None
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
        return {
            "routed": bool(voice and mic),
            "output": sinks.get(str(voice.get("sink"))) if voice else self.pactl("get-default-sink"),
            "microphone": sources.get(str(mic.get("source"))) if mic else self.pactl("get-default-source"),
            "muted": bool(mic and mic.get("mute")),
            "rate": (voice or {}).get("sample_specification", ""),
        }
