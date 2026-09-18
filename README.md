# ARM Firmware

Firmware images for the ARM device, and the updater that installs them.

## Updating your device

The easiest way is the **ARM Firmware Updater**: one window, no files to
download by hand.

1. Get `ARMUpdater.exe` from the
   [latest release](https://github.com/TactorumInc/ARM-Firmware-Public/releases/latest).
2. Plug the device in over USB.
3. Run the updater. It finds the device, lists every published firmware
   version, and shows the version currently installed. Pick one and click
   **Flash selected firmware**.

The updater confirms the new version is running when it finishes.
You can move to any release, newer or older, the same way.

Windows may show a SmartScreen warning the first time because the
executable is not code-signed: choose *More info* → *Run anyway*.

Full details, the cross-platform Python version, and troubleshooting are in
[`updater/README.md`](updater/README.md).

## Checking a board

`ARMDiagnostic.exe` (on every release) is the bring-up tool. It works through
the board a stage at a time - the controller, the load cell amplifier, the
motor carrier, each motor driver and the motors themselves - and saves a
report per board. Its **Motion** tab jogs the four axes from the keyboard and
is the way to check drivers, motors and wiring. **Before you use it, read the
hot-plug warning** - the drivers are destroyed by connecting or disconnecting
anything on the motor side under power. The full procedure, key map and
safety notes are in
[`updater/README.md`](updater/README.md#armdiagnosticexe---bring-up-and-testing-the-motors).

A **brand-new motor carrier** has no firmware of its own; the updater flashes
only the controller. The diagnostic says so in its carrier stage and its
STM32 tab does that first flash. Every later carrier update happens by itself
when the software connects.

## Releases

Every release on the [releases page](https://github.com/TactorumInc/ARM-Firmware-Public/releases)
carries:

| File | What it is |
|---|---|
| `firmware-merged_v<ver>.bin` | Complete image (bootloader + partition table + app). Flash at `0x0`. **This is what the updater uses.** |
| `firmware_v<ver>.bin` | Application only. Flash at `0x10000` |
| `partitions_v<ver>.bin` | Partition table. Flash at `0x8000` |
| `bootloader_v<ver>.bin` | Second-stage bootloader. Flash at `0x1000` |
| `ARMUpdater.exe` | The updater (Windows) |
| `ARMDiagnostic.exe` | Bring-up and test tool (Windows). Checks the board stage by stage, saves a report, and jogs the motion axes from its Motion tab; finds the device automatically |

Releases marked **pre-release** are development builds: functional, but not
validated for production use.

## Flashing by hand

If you would rather use [esptool](https://github.com/espressif/esptool)
directly:

```bash
pip install esptool
esptool --chip esp32 --port COM7 --baud 921600 write-flash 0x0 firmware-merged_v<ver>.bin
```

Always use the merged image at `0x0` unless you know the device already has
the current partition table (it changed in v2.1.0).

## After flashing

The device talks over its USB serial port at **921600 baud**. Send
`<SYSTEM:VERSION>` to read the installed version, `<SYSTEM:HELP>` for the
command list.
