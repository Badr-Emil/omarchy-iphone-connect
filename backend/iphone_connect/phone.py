"""Phone number validation, normalisation and masking."""

import re

_ALLOWED = re.compile(r"^\+?[0-9*#]+$")
_SEPARATORS = re.compile(r"[\s\-./()]")


class InvalidNumber(ValueError):
    pass


def normalize(number):
    """Return a dialable string (digits, leading +, * and #) or raise InvalidNumber.

    Spaces, dashes, dots, slashes and parentheses are removed. A leading "00" is
    kept as is: the phone network decides how to interpret it.
    """
    if number is None:
        raise InvalidNumber("no number given")
    cleaned = _SEPARATORS.sub("", str(number))
    if cleaned == "":
        raise InvalidNumber("no number given")
    if not _ALLOWED.match(cleaned):
        raise InvalidNumber(f"invalid characters in number: {number!r}")
    if "+" in cleaned[1:]:
        raise InvalidNumber("'+' is only allowed at the start")
    digits = re.sub(r"[^0-9]", "", cleaned)
    if len(digits) < 2 and not cleaned.endswith("#"):
        raise InvalidNumber("number too short")
    if len(digits) > 20:
        raise InvalidNumber("number too long (max 20 digits)")
    return cleaned


def is_valid(number):
    try:
        normalize(number)
        return True
    except InvalidNumber:
        return False


def mask(number):
    """Mask a number for logs/diagnostics: keep prefix and last two digits."""
    if not number:
        return ""
    value = str(number)
    digits = [i for i, ch in enumerate(value) if ch.isdigit()]
    if len(digits) <= 4:
        return "*" * len(value)
    keep = set(digits[:3] + digits[-2:])
    return "".join(ch if (i in keep or not ch.isdigit()) else "*" for i, ch in enumerate(value))


def valid_dtmf(tones):
    return bool(tones) and re.fullmatch(r"[0-9*#A-Da-d]+", tones) is not None
