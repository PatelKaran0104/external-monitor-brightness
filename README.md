# External Monitor Brightness

A small Windows tray app for dimming external monitors when their hardware brightness controls don't actually work.

It changes visible brightness via Windows display gamma ramps, so it works on any monitor Windows can detect, without DDC/CI or vendor utilities.

## Run it

Grab the latest `External Monitor Brightness.exe` from [Releases](../../releases) and double-click. No installer.

If you'd rather run from source, double-click `Run Brightness App.bat` — it sets up a venv on first run.

## Build the EXE

Double-click `Build EXE.bat`. Output ends up at `dist\External Monitor Brightness.exe`.

## Tests

```
python -m unittest discover -s tests -p "test_*.py" -v
```

## Notes

- Slider range is 50%-100%. Lower gamma values get rejected by most display drivers.
- Per-display brightness is saved and restored on next launch.
- Closing the window hides to tray so brightness stays applied. Use the tray icon's **Exit** to fully quit (which restores all displays to 100%).
- Optional global hotkeys: `Ctrl+Alt+Up` / `Ctrl+Alt+Down`.
- Optional run-at-startup toggle in the header.
- Light/dark theme toggle.
- State and logs live at `%LOCALAPPDATA%\BrightnessControl\`.
