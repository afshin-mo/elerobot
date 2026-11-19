import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"
LANGUAGES = ("ckb", "fa", "en")
FALLBACK_LANGUAGE = "en"


@lru_cache
def load_language(lang: str) -> dict[str, str]:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def t(lang: str | None, key: str, **params) -> str:
    """Translate a key. Falls back to English, then to the key itself (and logs it)."""
    for code in (lang, FALLBACK_LANGUAGE):
        if code in LANGUAGES:
            text = load_language(code).get(key)
            if text is not None:
                try:
                    return text.format(**params)
                except (KeyError, IndexError):
                    log.error("Bad placeholders for key %s in %s", key, code)
                    return text
    log.error("Missing translation key: %s", key)
    return key
