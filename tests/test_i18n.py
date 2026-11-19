import json
import string

import pytest

from app.core.i18n import I18N_DIR, LANGUAGES, t


def _load(code):
    return json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_all_languages_have_same_keys():
    reference = set(_load("en"))
    for code in LANGUAGES:
        assert set(_load(code)) == reference, code


def test_all_languages_use_same_placeholders():
    en = _load("en")
    for code in LANGUAGES:
        data = _load(code)
        for key, text in data.items():
            assert _placeholders(text) == _placeholders(en[key]), (code, key)


@pytest.mark.parametrize("code", LANGUAGES)
def test_no_empty_texts(code):
    assert all(text.strip() for text in _load(code).values())


@pytest.mark.parametrize("code", LANGUAGES)
def test_no_literal_backslash_n(code):
    """A text written with a doubled backslash would show \\n to the user instead of a new line."""
    assert [k for k, text in _load(code).items() if "\\n" in text] == []


def test_translate_with_params_and_fallbacks():
    assert "Hydroclass" in t("en", "welcome", company="Hydroclass")
    assert t(None, "role_manager") == "Manager"  # no language -> English
    assert t("xx", "role_manager") == "Manager"  # unknown language -> English
    assert t("en", "no_such_key") == "no_such_key"  # missing key -> the key
