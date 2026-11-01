"""Opt-in, owner-bound encrypted answers. No AI access or inferred demographic values."""

import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError

from jobs.models import SavedApplicationAnswers

FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_country",
        "city",
        "country",
        "linkedin",
        "notice_period",
        "work_authorization_notes",
        "gender_identity",
    }
)


def cipher():
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise ValidationError("Private-answer encryption is not configured.")
    try:
        return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise ValidationError("Private-answer encryption is not configured correctly.") from exc


def save_answers(owner_id: int, answers: dict, *, reuse_enabled: bool) -> None:
    if set(answers) - FIELDS or any(
        not isinstance(v, str) or len(v) > 2000 for v in answers.values()
    ):
        raise ValidationError("Unsupported application-answer fields.")
    payload = {
        "owner_id": owner_id,
        "version": 1,
        "answers": {k: v for k, v in answers.items() if v},
    }
    encrypted = cipher().encrypt(json.dumps(payload).encode()).decode()
    SavedApplicationAnswers.objects.update_or_create(
        owner_id=owner_id,
        defaults={
            "encrypted_answers": encrypted,
            "reuse_enabled": reuse_enabled,
        },
    )


def read_answers(owner_id: int) -> tuple[dict, bool]:
    record = SavedApplicationAnswers.objects.filter(owner_id=owner_id).first()
    if record is None:
        return {}, False
    try:
        payload = json.loads(cipher().decrypt(record.encrypted_answers.encode()))
        answers = payload["answers"]
        if (
            payload["owner_id"] != owner_id
            or payload["version"] != 1
            or not isinstance(answers, dict)
            or set(answers) - FIELDS
            or any(not isinstance(value, str) for value in answers.values())
        ):
            raise ValueError("Invalid answer envelope")
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValidationError("Saved answers could not be read. Re-enter or delete them.") from exc
    return answers, record.reuse_enabled


def prefill_contact(owner_id: int) -> dict:
    """Known equivalent contact fields only; terms and demographics are never filled here."""
    answers, reuse = read_answers(owner_id)
    return {
        key: value
        for key, value in answers.items()
        if reuse and key in {"first_name", "last_name", "email", "phone"}
    }
