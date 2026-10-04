# Contributing to darksign

Thanks for your interest! Issues and pull requests are welcome.

## Code

- Everything in the code is in English: identifiers, comments, docstrings,
  log messages. Commit messages too.
- Python 3 standard library plus the Debian packages listed in `install.sh`
  (`python3-flask`, `python3-mpv`, `python3-libgpiod`, `python3-pil`...): no
  pip dependency, so that the installer stays a single `apt-get install`.
- The target is a Raspberry Pi 3 (1 GB of RAM): keep the player light, and
  test what you touch on a real Pi when you can (playback, setup screen,
  network). Explain in the pull request what was tested and how.
- The scripts in `system/` run as root through sudo: they must not import
  any project code (the project folder is writable by the player user) and
  must validate every value they read.

## Translations

Every text shown to a user goes through a translation key. A language is
made of:

1. `locales/<code>.json`: web interface, server messages and the player's
   setup screen on the TV. Copy `locales/en.json` (the reference) to
   `locales/<code>.json`, set `"_meta": {"name": "..."}` to the language's own
   name (e.g. `"Deutsch"`), and translate the values. Keep the `{placeholders}`
   unchanged; `**...**` marks bold text; a few values contain simple HTML
   (`<b>`, `<code>`, links), keep the tags.
2. `install.sh`: the installer runs before the project is downloaded, so its
   texts live in the script itself. Add a `T_<code>` array next to `T_en` and
   `T_fr`, add the code to `LANGUAGES` and a line to the language question.
   The values are `printf` formats: keep each `%s`.

Check that nothing is missing or misspelt:

    python3 i18n.py check

It lists, for each language, the keys missing compared to English and the
unknown ones. A missing key falls back to English, so a partial translation
is still usable.

To see a language in action: the selector at the top of the web interface
switches the interface; the "Screen language" setting (section "Name,
language and network") switches the setup screen on the TV.

## Adding a feature with texts

- Web interface: static HTML gets a `data-i18n` (text), `data-i18n-html`,
  `data-i18n-placeholder` or `data-i18n-title` attribute; JavaScript uses
  `t("key", {vars})`.
- Python code: messages for the web interface are structured,
  `i18n.msg("key", name=value)`, and translated by the browser (each viewer
  sees their own language); the setup screen uses `i18n.t(key, lang)` with
  the player language. Log messages are plain English, not translated.
- Add the new keys to `locales/en.json` and to the other languages you can,
  then run `python3 i18n.py check`.

## License

By contributing, you agree that your contributions are licensed under the
[GNU General Public License v3.0](LICENSE).
