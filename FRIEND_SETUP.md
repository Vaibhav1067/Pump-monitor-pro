# Pump Health Console: hardware setup

Follow these in order. It takes about 30 minutes, plus a download on first use.

## Safety first

- **This software only warns. It cannot switch the pump off.** Keep a normal switch and breaker within reach, and don't rely on this as the pump's only protection.
- **Mains can kill.** If the pump runs on 230 V, have someone qualified do the wiring around the current sensor, and insulate it. Turn the power off at the breaker before touching anything.
- **Don't connect 5 V to the ESP32.** The current and pressure sensors output up to 5 V and need a voltage divider, or they can destroy the ESP32 pin.

## Wiring

**Use a classic ESP32 board** (ESP32-WROOM or ESP32 DevKit). Newer chips such as the S3 and C3 don't have the same pins, so this wiring won't work on them.

| Sensor | Connect to | Notes |
|---|---|---|
| ADXL345 vibration | SDA GPIO21, SCL GPIO22, 3.3 V, GND | Bolt it firmly to the pump body |
| DS18B20 temperature | GPIO4, 3.3 V, GND | 4.7 kΩ pull-up from GPIO4 to 3.3 V. Fix it to the motor body |
| ACS712 current | OUT through a divider to GPIO34 | 10 kΩ / 20 kΩ divider. Only the motor's live wire goes through it |
| Pressure transducer | OUT through a divider to GPIO35 | 10 kΩ / 20 kΩ divider |
| A3144 speed | GPIO26 | 10 kΩ pull-up to 3.3 V. One magnet on the shaft |
| Float switch | Between GPIO25 and GND | **Closed must mean water is OK** |

**The temperature and vibration sensors are required.** The ESP32 sends no readings at all until both answer, so connect them first.

**If your parts differ,** change the values marked `CAL` near the top of `firmware/esp32_pump_node/esp32_pump_node.ino` before flashing:

- `ACS_SENS_V_PER_A`: 0.100 for the 20 A ACS712, 0.185 for 5 A, 0.066 for 30 A
- `P_V_MIN`, `P_V_MAX`, `P_BAR_MAX`: from the pressure sensor's datasheet
- `HALL_MAGNETS`: the number of magnets on the shaft

## Install

1. **Install Python** from [python.org](https://www.python.org/downloads/). Tick **Add python.exe to PATH** in the installer.
2. **Install the Arduino tool.** Open Command Prompt and run:
   ```
   winget install ArduinoSA.CLI
   ```
3. **Unzip** this folder somewhere simple, such as `C:\PumpMonitor`. Don't run it from inside the zip.

## First run

1. **Load the firmware.** Plug in the ESP32 with a USB cable that carries data, then double-click **`flash.bat`**. The first run downloads about 400 MB. It should end with `firmware is working`.
2. **Check the sensors.** Start the pump, then double-click **`check.bat`**. It reads for 30 seconds and tells you what to fix, sensor by sensor. Repeat until it says `ALL SENSORS LOOK WIRED CORRECTLY`.
3. **Start the dashboard.** Double-click **`start.bat`** and leave the black window open. The page opens in your browser and should say **Live from the ESP32**.
4. **Click once anywhere on the page.** Browsers don't allow alarm sounds until you do.
5. **Let it learn.** With the pump running normally, it shows **Learning** for 2 minutes. **Only low-water alarms work during this time.**

If a blue **SIMULATED** banner shows, the numbers are made up and are not from your pump, however healthy they look. The banner names the problem and the fix.

**Before showing anyone,** do one full rehearsal: run the steps above, then partly close the outlet valve and confirm the warning appears and clears. The first run on new hardware is when problems show up.

## On a Raspberry Pi

The ESP32 plugs into the Pi's USB port. The steps above are for Windows, because `.bat` files don't run on a Pi. Use these instead, on Raspberry Pi OS Bookworm or newer.

1. **Load the firmware once.** The easiest way is `flash.bat` on any Windows PC, then move the ESP32 to the Pi.
2. **Install.** In a terminal on the Pi, in this folder, run:
   ```
   bash deploy/install_pi.sh --kiosk
   ```
   Leave out `--kiosk` if the Pi has no screen. The app then starts on every boot. Reboot once afterwards so the USB permission applies.
3. **Check the sensors.** Start the pump, then run:
   ```
   sudo systemctl stop pump-console
   .venv/bin/python tools/check_rig.py
   sudo systemctl start pump-console
   ```
4. **Open the dashboard.** It opens by itself with `--kiosk`. From another device on the same network, go to `http://<pi-address>:8000`. Running `hostname -I` on the Pi shows the address.
5. **Let it learn** for 2 minutes with the pump running normally.

Use the Pi's official power supply. Power the sensors' 5 V side properly too, because a weak supply can make the ESP32 restart when the motor starts. To see what the app is doing, run `journalctl -u pump-console -f`.

## What the alarms mean

| On screen | Meaning | What to do |
|---|---|---|
| **Red DANGER** banner, pulsing red edge, fast beep | Readings far from normal, low water, or no data from the pump | Follow the message. Stop the pump if it's safe to |
| **Amber WARNING** banner, pulsing amber edge, slow beep | Readings are drifting from normal | Inspect the pump soon |

**Silence** stops the beep only. The banner stays until the problem clears.

## Things to know

- **It learns from however the pump is running.** If the pump is already faulty during the learning period, it treats that as normal. Press **Learn normal again** in Controls after a repair, or if that happened.
- **Restarting `start.bat` starts the learning again.** So does changing the data source.
- **Check the alarms yourself.** Partly close the outlet valve and confirm you get a warning. Reopen it and confirm it clears.
- **Expect some false alarms at first.** The model was trained on a different pump, so its accuracy on this one is unknown.
- **Time to failure is an estimate.** It extends each sensor's trend to its danger limit and has not been tested on a pump that really wore out. Sudden faults give no advance warning. It restarts when the program restarts, so leave it running to catch slow wear.
- **Let the pump warm up before it learns.** A motor still warming up can raise false temperature warnings. If that happens, press **Learn normal again** once it has been running about 30 minutes.
- **Faults that mainly change water flow can be missed.** There is no flow sensor, so a partly blocked valve is only seen if pressure, current or vibration move too.

## If something goes wrong

| Problem | Fix |
|---|---|
| `python` not found | Reinstall Python with **Add python.exe to PATH** ticked, then reopen the window |
| No COM port, or "no ESP32 found" | Try another USB cable. Install the CP210x or CH340 driver for your board |
| Upload fails | Hold the board's **BOOT** button while it says `Connecting…` |
| "Port busy" | Close the dashboard and Arduino Serial Monitor. Only one program can use the port |
| "Sensor not found" | Check the wiring of the temperature sensor or accelerometer |
| Always shows low water | The float switch is open or reversed. Closed must mean water is OK |
| Readings are wrong but steady | Check the `CAL` values above and the dividers |

## Sending results back

To record a session, click **Start recording** on the dashboard. Send back the CSV file from the `logs` folder, together with a note of what the pump was doing and when.
