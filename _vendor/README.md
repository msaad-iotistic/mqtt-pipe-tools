# Vendored dependencies

Third-party libraries bundled directly into the repo so the tools run without
needing `pip` (many target boxes are minimal/embedded Linux where pip is broken
or absent).

## paho

`paho-mqtt` **1.6.1**, copied verbatim from PyPI. Pure Python, no third-party
runtime dependencies (stdlib only). License: EPL/EDL — see `paho/LICENSE.txt`.

`mqtt_cat.py` imports this only as a fallback: it tries the system-installed
`paho` first and uses this vendored copy if that import fails.

To update: `pip download paho-mqtt==<ver>`, replace `paho/`, drop `__pycache__`.
