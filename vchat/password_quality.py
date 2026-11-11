"""Quality rules for newly assigned local passwords."""

import re
import unicodedata

from wtforms.validators import ValidationError

WEAK_PASSWORD_MESSAGE = (
    "Пароль слишком предсказуем. Используйте несколько несвязанных слов "
    "или случайный пароль."
)
_COMMON = frozenset(
    """
    password passwords passwordpassword passw0rd qwerty qwertyuiop
    qwertyqwerty qwertyuiopasdf admin administrator adminadmin
    letmein letmeinletmein welcome welcomewelcome changeme changemechangeme
    iloveyou iloveyouiloveyou monkey dragon sunshine princess trustnoone
    correcthorsebatterystaple пароль парольпароль йцукен йцукенйцукен
    администратор админадмин
    """.split()
)
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "01234567890123456789",
    "qwertyuiopasdfghjklzxcvbnm",
    "йцукенгшщзхъфывапролджэячсмитьбю",
)
_LEET = str.maketrans("013457", "oieast")
_SYMBOL_LEET = str.maketrans("@$", "as")


def is_predictable_password(password: str) -> bool:
    """Detect common choices, repetitions and sequences without changing the secret."""
    normalized = unicodedata.normalize("NFKC", password).casefold()
    compact = "".join(char for char in normalized if char.isalnum())
    if len(set(compact)) < 4 or compact.isdecimal():
        return True

    core = re.sub(r"^[0-9]+|[0-9]+$", "", compact)
    substitutions = normalized.translate(_SYMBOL_LEET)
    substituted_compact = "".join(char for char in substitutions if char.isalnum())
    substituted_core = re.sub(r"^[0-9]+|[0-9]+$", "", substituted_compact)
    if not core or core in _COMMON or substituted_core.translate(_LEET) in _COMMON:
        return True
    if re.fullmatch(r"(.+?)\1+", compact) or re.fullmatch(r"(.+?)\1+", core):
        return True
    return any(core in sequence or core[::-1] in sequence for sequence in _SEQUENCES)


def validate_password_quality(_form, field) -> None:
    if not field.errors and is_predictable_password(field.data):
        raise ValidationError(WEAK_PASSWORD_MESSAGE)
