# ARM Firmware Updater

One window, three steps: pick a firmware version, confirm the device, click
Flash. The updater reads the list of published firmware releases straight
from GitHub, downloads the one you choose, and writes it to the device over
USB. You never handle a `.bin` file.

![flow](https://img.shields.io/badge/pick%20release-%E2%86%92%20detect%20device%20%E2%86%92%20flash-blue)

## Windows: the standalone app

1. Download **`ARMUpdater.exe`** from the latest release on the
   [releases page](https://github.com/TactorumInc/ARM-Firmware-Public/releases).
2. Plug the device in over USB.
3. Run `ARMUpdater.exe`.

On launch it fetches the release list and looks for the device on every
serial port. When it finds it, the port is filled in and the firmware
version currently on the device is shown next to it. Choose the release you
want from the drop-down (the newest is pre-selected and release notes appear
underneath), click **Flash selected firmware**, confirm, and watch the log.
When it finishes, the device reboots and the updater asks it for its
version to prove the new firmware is running.

Any release can be flashed, including older ones - switching back is the
same three steps.

**Windows SmartScreen** may show "Windows protected your PC" the first time,
because the executable is not code-signed. Click *More info*, then *Run
anyway*.

## Any platform: the Python script

```bash
pip install -r requirements.txt      # pyserial, esptool
python arm_updater.py
```

Options:

| Flag | Effect |
|---|---|
| `--port COM7` | Use this port and skip auto-detection |
| `--list` | Print the flashable releases and exit (no window) |
| `--repo owner/name` | Read releases from a different GitHub repo |

## How detection works

Every serial port is sent `<SYSTEM:PING>` at 921600 baud. The port that
answers in the ARM JSON protocol is the device, and `<SYSTEM:VERSION>` then
reads the installed version. If nothing answers - the device is running
something other than ARM firmware, or is bricked - the first port with a
recognised USB-serial bridge (FTDI, CP210x, CH340, Espressif) is offered as
an *unverified guess* in orange. Check it against Device Manager before
flashing.

Flashing does not depend on the running firmware at all: esptool talks to
the ESP32's ROM bootloader, so a device with corrupted firmware can still be
recovered. It refuses to write if the chip is not an original ESP32.

## What gets written

The release's `firmware-merged_v<ver>.bin` at flash offset 0x0. That single
image contains the bootloader, partition table, and application, so it is
correct whatever was on the device before - including devices from before
v2.1.0, when the partition layout changed.

Downloads are cached in `%LOCALAPPDATA%\ARMUpdater\cache\<tag>\`, so
re-flashing a version you have used before is instant and works offline.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "No serial ports found" | USB driver not installed for the board's serial bridge, or a charge-only cable |
| Device shown as *unverified guess* | Nothing answered the protocol. Fine to flash if you are sure it is the right port |
| "Failed to connect to ESP32" | Another program (serial monitor, jog GUI) has the port open; close it and retry. Some boards need the BOOT button held while the updater connects |
| Flash succeeds, "device did not answer" afterwards | Power-cycle the device and click *Detect device* |
| GitHub API error 403 | Rate limit (60 requests/hour unauthenticated). Wait a few minutes |

## ARMJog.exe - testing the motors

Releases also ship **`ARMJog.exe`**, a joystick-style jog GUI for the four
motion axes (X, Y, X2, Z). It finds the device the same way the updater
does; if nothing answers the protocol it asks you to pick the port.

### Never hot-plug - read this first

The motor drivers and the motor power supply must only ever be connected
or disconnected with **everything switched off**:

- **Never insert or remove a driver module** from its socket while the
  carrier is powered.
- **Never plug or unplug the DC end of the motor supply** (the lead into
  the carrier) while the supply is switched on. Connect it cold, then
  switch the supply on at the mains side; switch off at the mains side
  before unplugging.
- **Never connect or disconnect a motor** while its driver is powered.

Making or breaking the motor rail under power produces a voltage spike -
inductive kick from the motor windings and cable, inrush into the bulk
capacitors - that exceeds the drivers' ratings and destroys them
instantly, often with no visible sign until the axis is found dead. The
firmware reports a `VM_RESET` event if it sees the rail cycle, but that is
after the fact; the only protection is the order you do things in.

### Procedure

1. Everything off. Fit driver modules in the sockets of the axes you want,
   connect the motors, connect the DC supply lead to the carrier.
2. Switch the motor supply on (at the wall or its own switch), then
   connect USB.
3. Run `ARMJog.exe`. It finds the device.
4. In **Axis settings**, untick *Connect* for any socket with no driver
   fitted. The firmware leaves those axes disabled and the GUI ignores
   their keys.
5. Click **Connect**. The log shows `INIT: OK` and each connected axis's
   configuration is read back into the fields.
6. Set a small jog speed to start (1-2 mm/s). Click the Jog panel or press
   Esc so keystrokes go to the window, then hold a key to move:
   `← / →` X, `W / S` Y, `A / D` X2, `↑ / ↓` Z. Several axes can be held at
   once. The position readout updates live; release to stop.
7. Tune as needed - max speed, acceleration, run/hold current, StealthChop
   (silent) per axis - then **Apply + Save to device** to persist it.
8. Optional safety test: tick *Simulate comms fault*. Three seconds into a
   jog the GUI stops talking; the firmware must stop the axis on its own
   and a `HOST_TIMEOUT` event appears in the log. Use a low speed and clear
   travel.
9. Finished: **Disable all**, close the window, switch the supply off at the
   mains side, *then* unplug anything.

There is **no end-of-travel protection** on current hardware (limit
switches are not active). Keep jog speeds modest and a hand near the power
switch.

## Building the executables

From the private firmware repository:

```bash
pip install pyinstaller esptool pyserial
python scripts/build_tools.py          # -> release/ARMUpdater.exe, ARMJog.exe
```
