"""Translations: locales/<lang>.json files, shared by Python and the web UI.

Each file maps a key to a text with {name} placeholders, plus a "_meta"
entry ({"name": "Français"}). English (en.json) is the reference and the
fallback for any missing key. To add a language, copy en.json, translate the
values and check it with `python3 i18n.py check`.

Messages shown in the web UI but produced by the player or the web backend
are passed around as structured messages, {"key": ..., "vars": {...}}, and
translated by the browser in each viewer's language.
"""
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT = "en"


@lru_cache(maxsize=None)
def _load(lang):
    try:
        return json.loads((LOCALES_DIR / f"{lang}.json").read_text())
    except (OSError, ValueError):
        return {}


def languages():
    """Available languages: [{"code": "fr", "name": "Français"}, ...]."""
    out = []
    for path in sorted(LOCALES_DIR.glob("*.json")):
        meta = _load(path.stem).get("_meta", {})
        out.append({"code": path.stem, "name": meta.get("name", path.stem)})
    return out


def normalize(lang):
    """Closest available language code ("fr-FR" -> "fr"), else English."""
    codes = {l["code"] for l in languages()}
    lang = (lang or "").lower()
    for candidate in (lang, lang.split("-")[0].split("_")[0]):
        if candidate in codes:
            return candidate
    return DEFAULT


def t(key, lang=DEFAULT, **values):
    """Translated text for key; English, then the key itself, if missing."""
    text = _load(lang).get(key) or _load(DEFAULT).get(key) or key

    def value(m):
        v = values.get(m.group(1), m.group(0))
        return render(v, lang) if isinstance(v, dict) else str(v)   # nested
    return re.sub(r"\{(\w+)\}", value, text)


def msg(key, **values):
    """Structured message, translated later in the viewer's language."""
    return {"key": key, "vars": values}


def render(message, lang=DEFAULT):
    """Text of a structured message (or of a plain string, unchanged)."""
    if isinstance(message, dict) and "key" in message:
        return t(message["key"], lang, **message.get("vars", {}))
    return message


def check():
    """Report keys missing from, or unknown to, each language file."""
    reference = set(_load(DEFAULT)) - {"_meta"}
    ok = True
    for lang in languages():
        keys = set(_load(lang["code"])) - {"_meta"}
        missing, unknown = sorted(reference - keys), sorted(keys - reference)
        if missing or unknown:
            ok = False
        print(f"{lang['code']} ({lang['name']}): {len(keys)} keys"
              + (f", missing: {', '.join(missing)}" if missing else "")
              + (f", unknown: {', '.join(unknown)}" if unknown else ""))
    return ok


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(0 if check() else 1)
    # used by install.sh: python3 i18n.py LANG KEY [name=value ...]
    if len(sys.argv) >= 3:
        values = dict(arg.split("=", 1) for arg in sys.argv[3:])
        print(t(sys.argv[2], normalize(sys.argv[1]), **values))
    else:
        sys.exit(__doc__)
