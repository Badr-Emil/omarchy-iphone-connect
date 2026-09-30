"""Call audio routing through PipeWire (pipewire-pulse) and restore afterwards.

During a call two loopbacks are loaded:
  phone voice (bluez source)  -> current default sink   (PC speakers/headphones)
  current default source      -> phone (bluez sink)     (PC microphone)

Before that, the default sink/source are snapshotted to a state file. After the
call the loopbacks are unloaded and the defaults restored - also after a crash,
because the state file survives the process.
"""

import json
import os
import subprocess

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "iphone-connect")
STATE_FILE = os.path.join(STATE_DIR, "audio.json")


def _pactl(*args, runner=subprocess.run):
    result = runner(["pactl", *args], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class AudioRouter:
    def __init__(self, runner=subprocess.run, state_file=STATE_FILE):
        self.runner = runner
        self.state_file = state_file

    def pactl(self, *args):
        return _pactl(*args, runner=self.runner)

    # ---- snapshot / restore ----------------------------------------------

    def snapshot(self):
        return {
            "default_sink": self.pactl("get-default-sink"),
            "default_source": self.pactl("get-default-source"),
            "modules": [],
        }

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

    # ---- node discovery ----------------------------------------------------

    def phone_nodes(self, address):
        """Return (source, sink) names of the phone's HFP nodes, or (None, None)."""
        key = address.replace(":", "_").upper()
        source = sink = None
        for line in self.pactl("list", "short", "sources").splitlines():
            name = line.split("\t")[1] if "\t" in line else ""
            if name.startswith("bluez_") and key in name.upper() and not name.endswith(".monitor"):
                source = name
        for line in self.pactl("list", "short", "sinks").splitlines():
            name = line.split("\t")[1] if "\t" in line else ""
            if name.startswith("bluez_") and key in name.upper():
                sink = name
        return source, sink

    # ---- call lifecycle ------------------------------------------------------

    def start_call(self, address):
        state = self.load_state()
        if state and state.get("modules"):
            return state  # already routed
        state = state or self.snapshot()
        source, sink = self.phone_nodes(address)
        if not source or not sink:
            state["error"] = "phone audio nodes not found"
            self.save_state(state)
            return state
        pc_sink = state["default_sink"]
        pc_source = state["default_source"]
        modules = []
        modules.append(self.pactl("load-module", "module-loopback", f"source={source}", f"sink={pc_sink}",
                                  "latency_msec=40", "source_dont_move=true", "sink_dont_move=false"))
        modules.append(self.pactl("load-module", "module-loopback", f"source={pc_source}", f"sink={sink}",
                                  "latency_msec=40", "source_dont_move=false", "sink_dont_move=true"))
        state.update({"modules": modules, "phone_source": source, "phone_sink": sink,
                      "pc_sink": pc_sink, "pc_source": pc_source})
        state.pop("error", None)
        self.save_state(state)
        return state

    def end_call(self):
        state = self.load_state()
        if not state:
            return None
        for module in state.get("modules", []):
            try:
                self.pactl("unload-module", str(module))
            except RuntimeError:
                pass
        for kind, key in (("sink", "default_sink"), ("source", "default_source")):
            value = state.get(key)
            if value and not value.startswith("bluez_"):
                try:
                    self.pactl(f"set-default-{kind}", value)
                except RuntimeError:
                    pass
        self.clear_state()
        return state

    def set_mute(self, muted):
        state = self.load_state()
        if not state or not state.get("phone_sink"):
            raise RuntimeError("no call audio is active")
        self.pactl("set-sink-mute", state["phone_sink"], "1" if muted else "0")

    def is_muted(self):
        state = self.load_state()
        if not state or not state.get("phone_sink"):
            return False
        return self.pactl("get-sink-mute", state["phone_sink"]).lower().endswith("yes")

    def describe(self):
        state = self.load_state() or {}
        return {
            "routed": bool(state.get("modules")),
            "output": state.get("pc_sink") or self.pactl("get-default-sink"),
            "microphone": state.get("pc_source") or self.pactl("get-default-source"),
            "error": state.get("error"),
        }
