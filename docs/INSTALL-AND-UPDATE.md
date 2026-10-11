# Installing and updating the panel

The panel is a whole repository, not a single file: `panel_app.py` imports the
`panel_tools/` package, and the backend lives in `core/`. Always keep the checkout
together at `~/cyberdeck-repo` and update it with Git. Copying `panel_app.py` on its
own (the `scp` steps in some older update notes) leaves out `panel_tools/`, and the app
won't start.

## First install

On the Pi, as user `eli`:

```bash
sudo apt install git python3-picamera2 python3-numpy python3-pil python3-evdev fonts-dejavu-core
git clone https://github.com/elijahjtan1/cyberdeck.git ~/cyberdeck-repo
cd ~/cyberdeck-repo
sudo python3 setup_panel_touch.py   # Keeps the TFT touch from clicking the desktop
sudo reboot
```

Then start the app by hand once:

```bash
sudo python3 ~/cyberdeck-repo/panel_app.py
```

The first launch asks you to tap five calibration crosses. Photos, calibration and
options are saved in `~/cyberdeck-data/`, which is never part of the repository.

Optional extras:

| What | How | Guide |
| --- | --- | --- |
| Start at boot | **OPTIONS → Start at boot**, or `sudo python3 setup_panel_service.py --now` | [HOME-AND-STARTUP.md](HOME-AND-STARTUP.md) |
| Media keys and the "what is playing" visualizer | `sudo apt install playerctl pulseaudio-utils` | [APPS-AND-SAVERS.md](APPS-AND-SAVERS.md) |
| Real backlight dimming | Rewire the LED pin, then `sudo python3 setup_backlight.py` | [BACKLIGHT-PWM.md](BACKLIGHT-PWM.md) |
| Backend: history, offline search, AI answers | `sudo apt install python3-flask`, then `sudo python3 setup_core_service.py --now` | [CORE.md](CORE.md), [LIBRARIAN.md](LIBRARIAN.md) |

## Updating

Either tap **TOOLS → MORE → Updates → Check GitHub**, then **Update** and **Restart app**
(see [TOOLS.md](TOOLS.md#explicit-software-updates)), or in a terminal:

```bash
cd ~/cyberdeck-repo
git pull --ff-only
sudo systemctl restart cyberdeck-panel   # If it runs at boot; otherwise restart it by hand
sudo systemctl restart cyberdeck-core    # Only if the backend is installed
```

If Git reports local changes, stop and keep them (`git stash`) rather than resetting.
No update so far has needed a reboot, an SPI or GPIO change, or a new touch setup;
when one does, its guide says so.

## Running by hand

Only one copy can own the camera and touchscreen. Stop the service first:

```bash
sudo systemctl stop cyberdeck-panel
sudo python3 ~/cyberdeck-repo/panel_app.py
```

| Option | Use |
| --- | --- |
| `--calibrate` | Recalibrate touch at start (or use **OPTIONS → Calibrate touch**) |
| `--rotation 0\|90\|180\|270` | Turn the UI for this run only, if you can't reach OPTIONS → Screen |
| `--fps 10\|15\|20` | Camera preview rate |
| `--status-only` | Run without the camera |
| `--touch /dev/input/eventN` | Use a specific touch device |
| `--data FOLDER` | Use another data folder |
| `--backend URL\|off` | Point at the backend, or turn the link off |

## Going back

**TOOLS → MORE → Updates → Roll back** restores the previous update made from that
menu. From a terminal, `git log --oneline` lists versions and
`git checkout <revision>` loads one; `git switch main` returns to the latest.

## Legacy

`cyberdeck.py` is the first version of the app, kept as a fallback. Its own guide is
[SETUP.md](SETUP.md); don't run it at the same time as `panel_app.py`.
