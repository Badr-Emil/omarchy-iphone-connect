"""Call state machine.

IDLE -> DIALING -> ALERTING -> ACTIVE -> DISCONNECTED -> IDLE
IDLE -> INCOMING -> ACTIVE | DISCONNECTED
ACTIVE <-> HELD

IDLE -> ACTIVE/HELD is allowed so a call that was already running when the
phone connected (or when the backend started) is adopted instead of rejected.
"""

from enum import Enum


class CallState(str, Enum):
    IDLE = "idle"
    DIALING = "dialing"
    ALERTING = "alerting"
    INCOMING = "incoming"
    ACTIVE = "active"
    HELD = "held"
    DISCONNECTED = "disconnected"


TRANSITIONS = {
    CallState.IDLE: {CallState.DIALING, CallState.ALERTING, CallState.INCOMING, CallState.ACTIVE, CallState.HELD},
    CallState.DIALING: {CallState.ALERTING, CallState.ACTIVE, CallState.DISCONNECTED},
    CallState.ALERTING: {CallState.ACTIVE, CallState.DISCONNECTED},
    CallState.INCOMING: {CallState.ACTIVE, CallState.DISCONNECTED},
    CallState.ACTIVE: {CallState.HELD, CallState.DISCONNECTED},
    CallState.HELD: {CallState.ACTIVE, CallState.DISCONNECTED},
    CallState.DISCONNECTED: {CallState.IDLE},
}

# org.pipewire.Telephony.Call1.State values -> CallState
PIPEWIRE_STATES = {
    "dialing": CallState.DIALING,
    "alerting": CallState.ALERTING,
    "incoming": CallState.INCOMING,
    "waiting": CallState.INCOMING,
    "active": CallState.ACTIVE,
    "held": CallState.HELD,
    "disconnected": CallState.DISCONNECTED,
}


class InvalidTransition(Exception):
    pass


def from_pipewire(value):
    try:
        return PIPEWIRE_STATES[str(value).lower()]
    except KeyError:
        raise ValueError(f"unknown call state: {value!r}")


class CallStateMachine:
    def __init__(self):
        self.state = CallState.IDLE
        self.history = [CallState.IDLE]

    def can(self, target):
        return target == self.state or target in TRANSITIONS[self.state]

    def transition(self, target):
        target = CallState(target)
        if target == self.state:
            return False
        if target not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {target.value}")
        self.state = target
        self.history.append(target)
        return True

    def reset(self):
        self.state = CallState.IDLE
        self.history.append(CallState.IDLE)

    @property
    def in_call(self):
        return self.state in (CallState.DIALING, CallState.ALERTING, CallState.INCOMING, CallState.ACTIVE, CallState.HELD)

    @property
    def needs_audio(self):
        return self.state in (CallState.DIALING, CallState.ALERTING, CallState.ACTIVE, CallState.HELD)
