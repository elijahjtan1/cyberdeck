# Instructions for coding agents

This repository is worked on by more than one AI coding agent (Claude Code and ChatGPT
Codex) and by its owner, Eli. Read this file before changing anything. The task list and
who owns each item are in [docs/ROADMAP.md](docs/ROADMAP.md).

## The device

- Raspberry Pi 4 (4 GB), 64-bit Raspberry Pi OS, reachable as `eli@cyberdeck.local`.
- 2.8" ILI9341 SPI TFT (`fbtft`, 320x240 RGB565), ADS7846/XPT2046 resistive touch,
  Pi ribbon camera, optional PWM backlight.
- Installed on the Pi: kiwix-serve (offline Wikipedia), Ollama with Qwen3 0.6B.
  Open WebUI was too slow and is not used.
- Pending hardware: GPS module, larger touchscreen and keyboard, battery management,
  ESP32 (on hand, not yet integrated).
- The repository lives at `~/cyberdeck-repo` on the Pi. User data lives in
  `~/cyberdeck-data/` (photos, album, gifs, sprites, options) and is never committed.
- The panel runs as root from the `cyberdeck-panel.service` systemd unit (see
  `setup_panel_service.py`), because it needs the framebuffer, the touch device and
  the camera.

## Layout

| Path | What it is |
| --- | --- |
| `panel_app.py` | The touch panel app: pages, savers, apps, games, main loop |
| `panel_tools/` | Hardware and helper modules (camera, backlight, runtime, network, games, tools UI, updates) |
| `setup_*.py` | One-off installers (boot service, touch isolation, PWM backlight) |
| `tests/` | Hardware-free unittest suite using fake screen, touch and camera |
| `tools/render_*.py` | Regenerate the preview images in `docs/previews/` |
| `docs/` | User-facing guides, one per feature area, plus the roadmap |
| `cyberdeck.py` | Old first version, kept as a fallback. Don't extend it. |

New backend work (librarian, telemetry, GPS, ESP32) goes in a new `core/` package, as
described in the roadmap. Keep it importable without Pi hardware.

## Rules

1. **Tests must pass:** `python3 -m unittest discover -s tests -v`. Add tests for new
   behaviour. Hardware, network services (Kiwix, Ollama), serial ports and the clock are
   faked in tests; nothing in the suite may need a Pi, the internet or root.
2. **Lint:** `python3 -m pyflakes panel_app.py panel_tools tests tools` (plus `core` once it
   exists) should report nothing new.
3. **Offline first.** Nothing at runtime may fetch from the internet: vendor JavaScript,
   CSS and fonts into the repo. Only `panel_tools/updates.py` talks to GitHub, and only
   when the user asks.
4. **Dependencies:** standard library, Pillow, and packages available from Raspberry Pi
   OS `apt` (for example `python3-flask`, `python3-picamera2`). Ask the owner before
   adding anything that needs `pip` on the Pi.
5. **Be honest on screen.** Never show invented readings as real. Simulated GPS, battery
   or ESP32 data must be labelled **SIMULATED** wherever it appears. The librarian must
   say when the offline sources don't contain an answer.
6. **Light on the Pi.** CPU, RAM and SD-card writes are shared with Ollama. Avoid
   busy loops, batch writes to disk, and don't hold the camera when nothing needs it.
7. **Don't break the panel.** The touch panel must keep working if the backend, Kiwix or
   Ollama is down; fall back or show a message, never crash.
8. **Match the existing style:** PIL drawing on a logical canvas, layouts that work in both
   landscape (320x240) and portrait (240x320), plain functions and small classes, short
   comments that explain why.
9. **Docs:** user-visible changes update the matching guide in `docs/` (and `README.md` if
   a page or file is added). Regenerate previews with `python3 tools/render_previews.py`
   when screens change.
10. **No secrets or personal data** in commits: no tokens, Wi-Fi passwords, photos or logs.
    Don't put AI model names or version identifiers of the coding agent in commits, code
    or docs. (Ollama model names such as `qwen3:0.6b` are fine; they are configuration.)

## Working together

- **One branch per agent and task:** for example `claude/<topic>` or `codex/<topic>`.
  Never push to another agent's branch.
- **Changes reach `main` through pull requests** that the owner merges. Don't push
  directly to `main` or force-push to shared branches.
- **Claim work before starting it:** set yourself as owner of the item in
  `docs/ROADMAP.md` in your branch's first commit, so two agents don't build the same thing.
- **Keep pull requests small and focused**, one roadmap item each. The description says
  what changed, how it was tested, and what still needs checking on the Pi.
- **Reviewing:** agents may review each other's pull requests. Comments should name the
  concrete failure (input, what happens, what should happen), not style preferences.
- **Working on the Pi itself:** use your own clone or `git worktree`, not the checkout the
  boot service runs, and run as user `eli`, not root. Ask before running `sudo`, changing
  systemd units, or touching `/boot/firmware/config.txt`.
- **Merge conflicts in `docs/ROADMAP.md`:** keep both sides' status updates.
