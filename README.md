# R3 Rover Control Kit

A bilingual browser console for a WheelTec R3 Mecanum robot: manual drive, absolute X/Y moves, and preset or custom turns. The console starts in English and remembers the language selected with the **中文 / EN** button. Windows runs the local console; a Raspberry Pi 3 on the same network runs the motor-control agent.

Download `R3-Control-Kit.zip` from the [latest release](https://github.com/tsawke/r3-rover-control-kit/releases/latest). For a robot you are taking over, obtain its connection details and authorized Pi login from the owner. The download includes a local simulator; live control connects to the robot over your local network.

## Quick start

1. Extract the entire ZIP to a writable folder. Install [Python 3.10 or newer](https://www.python.org/downloads/windows/) and use a current Chrome or Edge browser. The optional key-login mode also uses the [Windows OpenSSH Client](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).
2. Choose how to sign in to the Pi. **Password** (default for a new setup) uses the Pi account's existing username and password, including an administrator account; no client key registration is required. To use **key** instead, run **Prepare-Key.cmd** and add only the printed `.pub` file to the Pi, or use an existing authorized key.
3. Join the Pi and Windows computer to the same network as described below. Use a 2.4 GHz Wi-Fi network or wired Ethernet; the network must allow devices to reach one another.
4. With the board in `Mec`, **Motor OFF**, and the USB data cable in C30D **UART3**, double-click **Setup-Pi.cmd**. Enter the Pi login, such as `r3@r3-pi.local`, or a login with its current IPv4 address. Select `password` or `key`. In password mode, type the password and confirm the Pi host fingerprint on first contact (or enter the verified fingerprint up front). Select the UART3 adapter from the list. The script uploads the agent, verifies the files, reads the board without sending commands, and saves the connection profile in `control.local.json`.
5. Double-click **Start-Control.cmd**. If using password mode, type the Pi password in the launcher window once per launch. It starts the server on `127.0.0.1:8770` and opens the browser. Keep the launcher window open. Both **Simulation** and **Live** are available. Use **Stop** before closing the launcher or changing the Pi configuration.

The bundle contains the client, Pi agent, and pySerial 3.5. Password mode automatically installs [Paramiko](https://pypi.org/project/paramiko/) on Windows in the kit directory on first setup, so allow internet access for that step. Normal operation uses the local network. The password stays in the local launcher and the encrypted SSH session; the profile stores connection settings, and the browser talks only to localhost.

## Join or change networks

### Installed Pi: keep the existing system

Connect a monitor and keyboard to the Pi, or use an existing wired session so that changing Wi-Fi does not cut off the only connection. Sign in as the Pi administrator. If the Wi-Fi country has not been set, set it first in `sudo raspi-config` under Localisation Options. For a regular password-protected network, run:

```sh
nmcli dev wifi list
sudo nmcli --ask dev wifi connect "YOUR_NETWORK_NAME"
hostname -I
```

Enter the Wi-Fi password at the Pi prompt. Connect Windows to the same network. If `<hostname>.local` does not resolve on Windows, use the Pi's IPv4 address from `hostname -I` or the router's device list. If multiple addresses appear, use the one on the shared network. See the [Raspberry Pi networking guide](https://www.raspberrypi.com/documentation/computers/configuration.html#wireless-connection).

Enable Secure Shell (SSH) on the Pi with `sudo systemctl enable --now ssh` if it is not already enabled. A recipient of the reference robot can skip the SSH server change when the owner has already enabled and verified password login. On a different Pi previously set up for keys only, use the local terminal or an existing administrator session. Add an SSH server drop-in, for example `sudoedit /etc/ssh/sshd_config.d/60-r3-control-password.conf`, with the following account-specific setting. Replace `YOUR_PI_USER` with the actual login name.

```text
Match User YOUR_PI_USER
    PasswordAuthentication yes
Match all
```

Validate with `sudo sshd -t` and reload with `sudo systemctl reload ssh`. Keep the current administrator session open while you test a separate password login. Use an account with serial access and keep SSH on the local network.

From the Pi's local terminal, get its fingerprint for **Setup-Pi.cmd**:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

For **key login**, add the Windows-generated public-key line to `~/.ssh/authorized_keys` in the intended login's home directory and preserve existing entries:

```sh
mkdir -p ~/.ssh
chmod 700 ~/.ssh
nano ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

For either login method, check `id -nG` for the Pi login. If it lacks the `dialout` group and the serial port is denied, an administrator can run `sudo usermod -aG dialout LOGIN_NAME`, replacing `LOGIN_NAME` with the actual user, then sign out and back in.

### New card: provision from Raspberry Pi Imager

Use [Raspberry Pi Imager](https://www.raspberrypi.com/documentation/computers/imager.html), select Raspberry Pi 3 and Raspberry Pi OS Lite (32-bit), and choose the correct card. In customisation, set a hostname (for example `r3-pi`), create an administrator (for example `r3`), enter the target Wi-Fi and country, and enable SSH. Select **Use password authentication** for account login, or **Use public key authentication** and import the `.pub` from **Prepare-Key.cmd**. Writing an image erases the selected card; back up an existing card before replacing its contents. Boot the Pi and continue at step 4.

### Move an already configured robot

After changing networks, rerun **Setup-Pi.cmd** with the current host or IPv4 address. The script verifies and reuses an unchanged release. Existing files in `~/r3_pose_control` and the C30D firmware are left unchanged; the kit installs into `~/r3-control-kit/releases`. For laptops with multiple adapters, enter the shared-network IPv4 address in the optional **PC IPv4** field, or leave it blank to use normal routing.

## Reference robot and settings

| Item | Recorded configuration |
| --- | --- |
| Chassis | WheelTec R3, four 75 mm Mecanum wheels, no suspension, four independent motor channels |
| Motor and encoder | Four MG513P30_12V geared DC motors; 500-line giant magnetoresistive (GMR) encoder configuration |
| Motor controller | WheelTec C30D V2.3, STM32F407VET6, vendor C30D 2.x GMR release dated 2026-09-21; display mode `Mec` |
| Power | 12 V nominal battery for C30D; separate regulated 5 V Micro-USB supply for the Pi. Never connect 12 V to the Pi. |
| Data | Pi USB-A to the middle C30D USB-C port marked `UART3`, 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control. UART1 is for programming, not driving. The setup script identifies a compatible USB serial adapter and verifies the selected board by reading its telemetry. |
| Installed Pi | Raspberry Pi 3 Model B Rev 1.2, Raspberry Pi OS Lite (32-bit), Raspbian GNU/Linux 13 (Trixie), image dated 2026-10-06; ARMv7 userspace reports `armv7l`; Python 3.13.5. The Pi account needs permission to open the serial port. |
| Command frame | At connection, origin `(0, 0)`; initial forward along `+X`, left along `+Y`; counterclockwise angles positive. Verified configuration uses turn sign `1` and strafe sign `1`. |
| Limits | Planar limit 55–100 mm/s; rotation limit 8–13.18°/s; default turn +10°; input range −360° to +360°; up to 3 m per coordinate move, up to 60 s per command. |
| Position and stop | Position window 0.02 m; live angle window 1.25°. These are controller tolerances, not measured ground accuracy. Voltage gate: 11.0 V to start, 10.55 V minimum while running, 13.0 V maximum. Manual hold expires after 0.45 s without renewal on the Pi; the Pi link watchdog expires after 0.80 s. |

The reference Pi supports password and public-key login. Its actual connection details and credentials are provided separately to authorized operators. For a new robot, use a Pi account you control and run setup to record that robot's adapter and motion signs.

## How control works

```text
Windows browser -> local Python console -> SSH over the LAN -> Pi motion agent
Pi motion agent -> USB serial / UART3 -> C30D motor controller -> four wheels
C30D velocity and status telemetry -> Pi odometry -> browser position estimate
```

The browser sends structured requests to the Python service on `127.0.0.1`. Simulation runs on the Windows computer without a Pi. In Live mode, the console starts one SSH session with the Pi agent. The setup script installs the agent into a checksum-named release directory in the Pi user's home folder. It leaves the operating system and C30D firmware in place.

The Pi sends desired body-frame velocity: forward speed, sideways speed, and yaw rate. The C30D's vendor firmware maps these requests to the four wheels and closes the wheel-speed loops. The Pi decodes the standard 24-byte feedback frames and integrates the reported body velocity into a fixed ground-frame pose. The map shows this encoder-based estimate. Wheel slip and floor conditions can change real position without a corresponding change in the estimate.

For a coordinate move, the Pi computes the remaining ground-frame displacement and transforms it into the robot's current body frame. It limits speed and attempts to hold the starting heading. A turn tracks the requested relative angle, slows before the target, and waits for a quiet reading before declaring success. While a jog button is held, the chosen direction runs relative to the current robot heading. A second command cannot take over a moving session.

The Pi checks feedback freshness, voltage and the stop flag before motion. During motion it also checks that new telemetry arrives and that the wheels report movement. Loss of feedback, loss of the desktop heartbeat, or a stop request ends the motion session and attempts to send zero velocity. In manual mode the browser must renew the hold; releasing the button, leaving it, or losing focus ends the hold. If the Pi loses power or the USB cable is interrupted, the software may be unable to send the final stop frame. The black C30D main power switch remains the physical cutoff.

## Operate the console

**Simulation** is selected at startup. Try **Hold to drive**, a coordinate move, or a rotation. If you have not set up a Pi yet, run `Run-Kit.cmd simulation` from the extracted folder for an explicit simulation-only preview.

For the first **Live** test, support the chassis securely with all wheels clear. Set the physical origin and facing direction, keep clear of the wheels, and stand by the black main power switch. Verify the wheels remain stationary with Motor ON, confirm the hardware state in the page, and select **Connect to robot**. Press and release one direction at a time to verify wheel directions and stopping. For ground tests, mark the origin and compare short coordinate moves and turns against a ruler and angle marks. The display reports an estimate, not absolute ground position.

**Stop** ends the current session. On reconnect, re-align the origin. If the robot does not stop, cut C30D main power; a software stop depends on live power and the Pi's serial connection. Use live mode with an operator present.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Cannot reach the Pi | Check its power and IP address. Use wired Ethernet or a network that allows local device-to-device traffic. Keep the console on the same network. |
| SSH rejects login | In password mode, check the Pi username, password, and whether the Pi accepts password authentication. In key mode, check the public key in that login's `authorized_keys`. Verify a changed host fingerprint on the Pi before rerunning setup. |
| No UART3 adapter or no valid feedback | Use a USB data cable, the middle UART3 port, board power, and `Mec`. Check `dialout` permissions and stop any other serial controller before retrying. |
| Want to check the board without motion | Keep Motor OFF and run **Check-Pi.cmd**. A valid check prints voltage, stop flag, stationarity and `0 command frames sent`. |
| Startup port is busy | Use the original console tab or close the old launcher. To change port, run **Setup-Pi.cmd** again and enter another local port. |
| Browser reports a disconnect | Read the fault shown above the controls, check that the robot stopped, then reconnect from the marked origin. |

The package does not flash motor firmware, overwrite the operating system, or publish a control endpoint to the internet.

## Build and test

The controller, setup tools, and browser UI are all in this repository. `control_kit.py` handles first-time setup and startup; `r3_ui.py` serves the loopback console and runs the simulation or the SSH client; `r3_remote.py` and `r3_live.py` run the Pi motion session. The `ui/` directory contains the browser assets.

Run the offline tests and build the ZIP from the repository root:

```powershell
python -B -m unittest discover
python -B build_kit.py
```

The build downloads the pinned pySerial wheel and its license. It writes `dist/R3-Control-Kit.zip` and prints the archive's SHA-256 checksum. The build uses an explicit list of files; private credentials, local configuration, logs, and firmware are not distribution inputs. Icon notices are in [THIRD-PARTY-LUCIDE.txt](THIRD-PARTY-LUCIDE.txt).
