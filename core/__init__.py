"""cyberdeck-core: the local backend (telemetry, librarian, GPS; ESP32 later).

Modules here must import without Pi hardware or Flask; only core.server needs Flask.
See docs/CORE.md and docs/ROADMAP.md.
"""
VERSION = '0.1'
DEFAULT_PORT = 8765
