"""Phone number normalization (E.164) built on Google's libphonenumber port."""

from __future__ import annotations

import phonenumbers

from .errors import ValidationFailedError


def normalize_e164(raw: str, *, default_region: str = "CA") -> str:
    """Return the E.164 form of ``raw`` or raise :class:`ValidationFailedError`."""
    candidate = (raw or "").strip()
    if candidate.lower().startswith("tel:"):
        candidate = candidate[4:]
    if candidate.lower().startswith("sip:"):
        user = candidate[4:].split("@", 1)[0]
        candidate = user
    try:
        parsed = phonenumbers.parse(candidate, default_region)
    except phonenumbers.NumberParseException as e:
        raise ValidationFailedError(f"invalid phone number: {e}") from e
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        raise ValidationFailedError("invalid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def try_normalize_e164(raw: str | None, *, default_region: str = "CA") -> str | None:
    if not raw:
        return None
    try:
        return normalize_e164(raw, default_region=default_region)
    except ValidationFailedError:
        return None


def national_digits(e164: str) -> str:
    parsed = phonenumbers.parse(e164, None)
    return str(parsed.national_number)


def is_allowed_destination(e164: str, allowed_prefixes: list[str]) -> bool:
    return not allowed_prefixes or any(e164.startswith(p) for p in allowed_prefixes)
