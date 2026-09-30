"""Collect a diagnostics report. Phone numbers and device addresses are masked by default."""

import re
import subprocess

from . import phone

COMMANDS = [
    ("BlueZ version", ["bluetoothctl", "--version"]),
    ("bluetooth.service", ["systemctl", "is-active", "bluetooth"]),
    ("Bluetooth adapter", ["bluetoothctl", "show"]),
    ("Paired devices", ["bluetoothctl", "devices", "Paired"]),
    ("PipeWire", ["pipewire", "--version"]),
    ("pipewire (user)", ["systemctl", "--user", "is-active", "pipewire"]),
    ("wireplumber (user)", ["systemctl", "--user", "is-active", "wireplumber"]),
    ("iphone-connect (user)", ["systemctl", "--user", "is-active", "iphone-connect"]),
    ("Telephony D-Bus service", ["busctl", "--user", "tree", "org.pipewire.Telephony"]),
    ("oFono", ["busctl", "status", "org.ofono"]),
    ("Bluetooth audio cards", ["pactl", "list", "short", "cards"]),
    ("Sources", ["pactl", "list", "short", "sources"]),
    ("Sinks", ["pactl", "list", "short", "sinks"]),
    ("Backend status", ["iphone-connect", "status"]),
]

MAC = re.compile(r"\b([0-9A-F]{2})[:_]([0-9A-F]{2})[:_]([0-9A-F]{2})[:_][0-9A-F]{2}[:_][0-9A-F]{2}[:_][0-9A-F]{2}\b", re.I)
NUMBER = re.compile(r"\+?\d[\d \-]{6,}\d")


def scrub(text):
    text = MAC.sub(lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)}:XX:XX:XX", text)
    return NUMBER.sub(lambda m: phone.mask(m.group(0)), text)


def run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        output = (result.stdout + result.stderr).strip()
        return output or f"(exit {result.returncode}, no output)"
    except FileNotFoundError:
        return "(command not found)"
    except subprocess.TimeoutExpired:
        return "(timed out)"


def report(unmask=False):
    parts = []
    for title, command in COMMANDS:
        output = run(command)
        if title == "oFono" and ("not found" in output.lower() or "no such" in output.lower() or "not activatable" in output.lower()):
            output = "not running (not needed: PipeWire provides telephony)"
        if title == "Bluetooth adapter":
            output = "\n".join(l for l in output.splitlines() if re.search(r"Controller|Name|Powered|Pairable|UUID: Hands", l))
        parts.append(f"## {title}\n{output}\n")
    text = "\n".join(parts)
    return text if unmask else scrub(text)
