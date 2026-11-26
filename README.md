# R3 Rover Control Kit

A bilingual browser console for a WheelTec R3 Mecanum robot: manual drive, absolute X/Y moves, and preset or custom turns. The console starts in English and remembers the language selected with the **中文 / EN** button. Windows runs the local console; a Raspberry Pi 3 on the same network runs the motor-control agent.

Download `R3-Control-Kit.zip` from the [latest release](https://github.com/tsawke/r3-rover-control-kit/releases/latest). Access to this private project must be granted by the repository owner.

## Quick start

1. Extract the entire ZIP to a writable folder. Install [Python 3.10 or newer](https://www.python.org/downloads/windows/) and use a current Chrome or Edge browser. The optional key-login mode also uses the [Windows OpenSSH Client](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).
2. Choose how to sign in to the Pi. **Password** (default for a new setup) uses the Pi account's existing username and password, including an administrator account; no client key registration is required. To use **key** instead, run **Prepare-Key.cmd** and add only the printed `.pub` file to the Pi, or use an existing authorized key.
3. Join the Pi and Windows computer to the same network as described below. Use a 2.4 GHz Wi-Fi network or wired Ethernet; the network must allow devices to reach one another.
4. With the board in `Mec`, **Motor OFF**, and the USB data cable in C30D **UART3**, double-click **Setup-Pi.cmd**. Enter the Pi login, such as `r3@r3-pi.local`, or a login with its current IPv4 address. Select `password` or `key`. In password mode, type the password and confirm the Pi host fingerprint on first contact (or enter the verified fingerprint up front). Select the UART3 adapter from the list. The script uploads the agent, verifies the files, reads the board without sending commands, and saves the connection profile in `control.local.json`.
5. Double-click **Start-Control.cmd**. If using password mode, type the Pi password in the launcher window once per launch. It starts the server on `127.0.0.1:8770` and opens the browser. Keep the launcher window open. Both **Simulation** and **Live** are available. Use **Stop** before closing the launcher or changing the Pi configuration.

The bundle contains the client, Pi agent, pySerial 3.5 and its license. Password mode automatically installs [Paramiko](https://pypi.org/project/paramiko/) on Windows in the kit directory on first setup, so allow internet access for that step. Normal operation uses the local network. The password is not saved in the profile or sent to the browser; the launcher serves localhost only.

## Join or change networks

### Installed Pi: keep the existing system

Connect a monitor and keyboard to the Pi, or use an existing wired session so that changing Wi-Fi does not cut off the only connection. Sign in as the Pi administrator. If the Wi-Fi country has not been set, set it first in `sudo raspi-config` under Localisation Options. For a regular password-protected network, run:

```sh
nmcli dev wifi list
sudo nmcli --ask dev wifi connect "YOUR_NETWORK_NAME"
hostname -I
```

Enter the Wi-Fi password at the Pi prompt. Connect Windows to the same network. If `<hostname>.local` does not resolve on Windows, use the Pi's IPv4 address from `hostname -I` or the router's device list. If multiple addresses appear, use the one on the shared network. See the [Raspberry Pi networking guide](https://www.raspberrypi.com/documentation/computers/configuration.html#wireless-connection).

Enable Secure Shell (SSH) on the Pi with `sudo systemctl enable --now ssh` if it is not already enabled. For **password login** on a Pi previously set up for keys only, use the local Pi terminal or an existing administrator session. Set `PasswordAuthentication yes` in an appropriate SSH server configuration drop-in (for example, `sudoedit /etc/ssh/sshd_config.d/00-r3-password.conf`). Preserve the existing key settings. Validate with `sudo sshd -t`, check `sudo sshd -T | grep '^passwordauthentication'` reports `yes`, and run `sudo systemctl reload ssh`. If it still reports `no`, review the existing drop-ins and account-specific rules. Keep an existing session open until a separate login works. Do not forward the SSH port to the internet.

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
| Power and data | 12 V nominal battery for C30D; separate 5 V Micro-USB supply for Pi 3. Pi USB-A to middle C30D USB-C UART3 data port, 115200 baud. UART1 is not the control port. Never connect 12 V to the Pi. |
| Installed Pi | Raspberry Pi 3, Raspberry Pi OS Lite (32-bit), image dated 2026-10-06, hostname `r3-pi`, administrator `r3lab`; configured for SSH public-key authentication |
| Command frame | At connection, origin `(0, 0)`; initial forward along `+X`, left along `+Y`; counterclockwise angles positive. Verified configuration uses turn sign `1` and strafe sign `1`. |
| Limits | Planar limit 55–100 mm/s; rotation limit 8–13.18°/s; default turn +10°; input range −360° to +360°; up to 3 m per coordinate move, up to 60 s per command. |
| Position and stop | Position window 0.02 m; live angle window 1.25°. These are controller tolerances, not measured ground accuracy. Voltage gate: 11.0 V to start, 10.55 V minimum while running, 13.0 V maximum. Manual hold expires after 0.45 s without renewal on the Pi; the Pi link watchdog expires after 0.80 s. |

Existing operator credentials for the reference Pi are kept in a separate private handover README. Recipients who only need the software should create their own login and key.

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

The build downloads the pinned pySerial wheel and its license. It writes `dist/R3-Control-Kit.zip` and prints the archive's SHA-256 checksum. The build uses an explicit list of files; private credentials, local configuration, logs, and firmware are not distribution inputs.
