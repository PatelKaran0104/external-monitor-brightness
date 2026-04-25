# External Monitor Brightness

Windows release candidate for controlling visible brightness on one or more displays.

A small Windows desktop app for adjusting visible brightness on detected displays.

It uses Windows display gamma controls instead of monitor hardware brightness, because this setup reports hardware brightness changes but does not visibly apply them.

## Use it

Double-click `Run Brightness App.bat`.

The first run creates a local Python environment and installs dependencies. After that, it starts quickly.

## Build an EXE

Double-click `Build EXE.bat`. The finished app will be created at:

`dist\External Monitor Brightness.exe`

## Run Tests

Run focused core tests:

`python -m unittest discover -s tests -p "test_*.py" -v`

## Notes

- The slider range is intentionally limited to 50%-100%; this display driver rejects lower gamma values.
- Last used brightness levels are saved per display and restored on next launch.
- State is written atomically and backed up at `%LOCALAPPDATA%\BrightnessControl\state.json.bak` to reduce corruption risk.
- Tray quick actions are available (Show/Hide, Refresh, and set all displays to 50/75/100).
- Optional global hotkeys are available: `Ctrl+Alt+Up` and `Ctrl+Alt+Down`.
- Optional Run at startup toggle is available in the header.
- Theme toggle (light/dark) is available in the header.
- About / Help panel includes quick usage guidance and file locations.
- The layout adapts to connected monitors and only scrolls when there are more cards than fit on screen.
- Identify now shows a larger overlay for 3 seconds with monitor details.
- If no monitor is detected, the app shows an explicit empty state instead of a blank list.
- Closing the window hides the app to the tray so brightness stays applied; use the tray icon's Exit item to fully quit (which restores all displays to 100%).
- Logs are written to `%LOCALAPPDATA%\BrightnessControl\app.log`.
