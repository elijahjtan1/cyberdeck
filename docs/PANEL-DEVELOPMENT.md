# Working on the panel code

How `panel_app.py` and `panel_tools/` fit together, for anyone (person or coding agent)
adding a page, an app or a screensaver. Rules for contributors are in
[AGENTS.md](../AGENTS.md); installing on the Pi is in [INSTALL-AND-UPDATE.md](INSTALL-AND-UPDATE.md).

## The screen

- **Logical canvas.** Everything is drawn with PIL on a `W x H` canvas: 320x240 in
  landscape, 240x320 when the UI is turned 90° or 270°. `set_rotation()` changes `W`, `H`
  and `ROTATION`; `physical_image()` turns the finished picture for the 320x240
  framebuffer, and `to_logical()` maps raw touch back. Page code never thinks about
  rotation, only about `W`, `H` and `portrait()`.
- **Two layouts.** Every page must work in both shapes. Helpers keep this short:
  `content_bottom()` (lowest y above the tab bar), `row_rects()` (a row of buttons),
  `grid_rects()` (the two-column button grid of Options, Savers and Apps), `tab_rects()`.
- **Text.** Fonts are `FONT` (15), `SMALL` (12), `TINY` (10), `HUGE` (46).
  `fit_text()` truncates with "...", `wrap_text()` wraps to at most two lines, and
  `centred()` centres a line. `button()` shrinks, wraps or truncates its label to fit.
- **Colours.** `BG`, `PANEL`, `TEXT`, `MUTED`, `ACCENT`, `WARN`, `DANGER` at the top of
  `panel_app.py`. Some pages still use literal hex colours (see Known debt).

## Pages, buttons and taps

- `render(page, ...)` draws one page and returns the image. Inside, `render_page()` is
  one long `if page == ...` chain; the camera page caches its static parts in
  `CAMERA_CHROME` because it redraws up to 20 times a second.
- **Hotspots.** `button(..., action='kind:arg')` both draws a button and registers its
  rectangle in `HOTSPOTS`. A tap is resolved by `hit(x, y)` against exactly what was
  drawn last, and `HOTSPOTS_PAGE` stops a stale list being used after a page change.
  So a button that is drawn is always tappable, and one that isn't drawn can't be.
- **Actions** are strings such as `tab:status`, `opt:3`, `app:games`. The main loop in
  `main()` splits them with `partition(':')` and handles each kind.
- **Tabs.** `TABS` is the bottom bar; `TAB_PARENT` says which tab lights up for a sub-page
  (for example `history` lights STATUS).

## Apps, games and tools

Full-screen apps are objects in the `apps` dict in `main()`, keyed by page name. Each has:

| Member | Meaning |
| --- | --- |
| `render(now, wall_time, alerts)` | Returns the image to show |
| `tap(x, y, now)` | Handles a tap; return `'exit'` to leave the app, otherwise `None` |
| `delay` | Seconds until the next redraw is needed; keep it as long as possible (rule 6) |
| `hold` (optional) | Touch-and-hold support, used by some games |

Apps use their own `rects()` and the module function `index_at()` for taps rather than
`HOTSPOTS`. To add one: write the class, add it to `apps` in `main()`, add a tile to
`APP_TILES` (and to `HOME_TILES` if it belongs on the Home page), a note in `app_notes()`,
and exclude it from `saver_due()` if a screensaver must never cover it.
`panel_tools/ask.py` (`AskApp`) is a complete example that lives outside `panel_app.py`.
Games share `GameBase`, `game_header()` and `draw_game_dialog()`; their rules live in
`panel_tools/games.py` so tests can drive them without drawing.

`panel_tools/ui.py` (`ToolsUI`) is the TOOLS app: a page per tool, chosen by
`self.page`, with its own `rects` list. Add a tool by adding a `('Label', 'go:name')`
menu entry and an `elif self.page == 'name':` branch; put the logic in its own
`panel_tools/` module (as `sky.py` does) so it can be tested alone.

## Screensavers

A saver implements `render(now, wall_time, alerts) -> (image, seconds_to_next_frame)`,
`tap(x, y, now)` (return `False` to wake) and `close()`. `make_saver()` in `main()`
builds one from `options['saver_style']`; `saver_due()` decides when one starts.
The first tap after a saver only wakes the panel, so it never presses a button.

## Options

`DEFAULT_OPTIONS` holds every setting and `OPTION_VALUES` the allowed values; a tap
cycles to the next one. `load_options()` keeps only values that are in `OPTION_VALUES`,
so a bad or old `options.json` can never crash the app. Settings that aren't a fixed
list (such as `location.json` for Sun & moon) live in their own file in the data
folder and are validated where they're read.

## Background work and hardware

- `Stats.read()` samples CPU, RAM and temperature (through `core/sysinfo.py`) and merges
  in `HealthPoller`, a thread that reads Wi-Fi and throttling every 10 s and clock sync about once a minute.
  The result is the `rows` dict that pages draw from.
- `CameraWorker` (`panel_tools/runtime.py`) owns the camera on its own thread; only one
  process can hold it. `Job` runs one slow task (decode, update, save) off the UI thread.
- `Brightness` decides between real PWM dimming (`panel_tools/backlight.py`) and
  darker pixels. `BACKLIGHT_PWM` tells pages which one is in use.
- `panel_tools/backend.py` talks to `cyberdeck-core` and keeps working when it's down.

## Tests and previews

- `python3 -m unittest discover -s tests -v` runs everything without a Pi; screens,
  touch, camera and commands are faked. Render pages in both rotations in a test, and
  check that hotspots stay on the canvas.
- `python3 tools/render_previews.py` and `python3 tools/render_tools.py` redraw the
  images in `docs/previews/` from the real drawing code. Run them when a screen changes,
  and look at the images: most layout bugs show up there before they reach the Pi.

## Known debt

Worth knowing before a larger change; none of it is urgent.

- `panel_app.py` is about 4,600 lines. `render_page()` and `main()` (about 770 lines of
  nested functions sharing state through `nonlocal`) are the hardest parts to change
  safely. New features go in `panel_tools/` modules where possible.
- Many colours are literal hex values rather than the named constants, which is what
  makes a night palette (roadmap 7.10) a large change.
- `panel_tools/ui.py` is written in a denser style than the rest (several statements
  per line), which makes diffs there harder to review.
- Several guides in `docs/` began as per-update release notes; their "Install this
  update" sections now point to [INSTALL-AND-UPDATE.md](INSTALL-AND-UPDATE.md).
