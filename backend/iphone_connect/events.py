"""Map org.pipewire.Telephony D-Bus traffic to backend events.

All functions take plain Python values (already unpacked from GVariant) so
they can be unit tested without a bus.
"""

AG_IFACE = "org.pipewire.Telephony.AudioGateway1"
TRANSPORT_IFACE = "org.pipewire.Telephony.AudioGatewayTransport1"
CALL_IFACE = "org.pipewire.Telephony.Call1"

# HFP codec ids (Bluetooth HFP spec): 1 = CVSD, 2 = mSBC, 3 = LC3-SWB
CODECS = {1: "CVSD", 2: "mSBC", 3: "LC3-SWB"}


def codec_name(codec_id):
    if not codec_id:
        return ""
    return CODECS.get(int(codec_id), f"codec {codec_id}")


def is_wideband(codec_id):
    return codec_id is not None and int(codec_id) >= 2


def interfaces_added(path, interfaces):
    events = []
    if AG_IFACE in interfaces:
        props = interfaces[AG_IFACE]
        events.append({"type": "phone-connected", "path": path, "address": props.get("Address", "")})
    if TRANSPORT_IFACE in interfaces:
        props = interfaces[TRANSPORT_IFACE]
        events.append(_transport_event(path, props))
    if CALL_IFACE in interfaces:
        props = interfaces[CALL_IFACE]
        events.append({
            "type": "call-added",
            "path": path,
            "state": props.get("State", ""),
            "number": props.get("LineIdentification", ""),
            "name": props.get("Name", ""),
        })
    return events


def interfaces_removed(path, interfaces):
    events = []
    if CALL_IFACE in interfaces:
        events.append({"type": "call-removed", "path": path})
    if AG_IFACE in interfaces:
        events.append({"type": "phone-disconnected", "path": path})
    return events


def properties_changed(path, interface, changed):
    events = []
    if interface == CALL_IFACE:
        if "State" in changed:
            events.append({"type": "call-state", "path": path, "state": changed["State"]})
        if "LineIdentification" in changed or "Name" in changed:
            events.append({
                "type": "caller-id",
                "path": path,
                "number": changed.get("LineIdentification"),
                "name": changed.get("Name"),
            })
    elif interface == TRANSPORT_IFACE:
        events.append(_transport_event(path, changed))
    elif interface == AG_IFACE:
        volume = {k: changed[k] for k in ("SpeakerVolume", "MicrophoneVolume") if k in changed}
        if volume:
            events.append({"type": "volume", "path": path, **volume})
    return events


def _transport_event(path, props):
    event = {"type": "audio", "path": path}
    if "State" in props:
        event["state"] = props["State"]
    if "Codec" in props:
        event["codec"] = codec_name(props["Codec"])
        event["wideband"] = is_wideband(props["Codec"])
    return event


def ag_path_of(call_path):
    """/org/pipewire/Telephony/ag0/call1 -> /org/pipewire/Telephony/ag0"""
    return call_path.rsplit("/", 1)[0]
