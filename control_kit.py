"""Prepare, provision, check, and launch the R3 Control Kit."""

import argparse
from hashlib import sha256
import io
import ipaddress
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "control.local.json"
KEY = Path.home() / ".ssh" / "r3_control_kit_ed25519"
WHEEL = "pyserial-3.5-py2.py3-none-any.whl"
WHEEL_SHA256 = "c4451db6ba391ca6ca299fb3ec7bae67a5c55dde170964c7a14ceefec02f2cf0"
PI_FILES = ("r3_remote.py", "r3_live.py", "r3_control.py", "c30d_bench.py", "c30d_protocol.py")
DEFAULT_PORT = 8770
SERIAL = r"[A-Za-z0-9_.:-]{1,80}"

INSTALLER = """\
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from zipfile import ZipFile

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required on the Pi.")
digest = sys.argv[1]
data = sys.stdin.buffer.read()
if sha256(data).hexdigest() != digest:
    sys.exit("Pi payload checksum mismatch.")
releases = Path.home() / "r3-control-kit" / "releases"
releases.mkdir(parents=True, exist_ok=True)
release = releases / digest
with ZipFile(BytesIO(data)) as archive:
    if release.exists():
        if not all((release / name).is_file() and (release / name).read_bytes() == archive.read(name)
                   for name in archive.namelist()):
            sys.exit(f"Installed release has changed: {release}. Restore it before retrying.")
    else:
        with TemporaryDirectory(dir=releases) as temp:
            stage = Path(temp) / "agent"
            archive.extractall(stage)
            stage.rename(release)
print("Pi release verified:", digest)
"""


def validate(config, *, deployed=True):
    host = config.get("host", "")
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Enter the Pi as username@hostname.local or username@IPv4-address.")
    identity = config.get("identity")
    if not isinstance(identity, str) or not identity or not Path(identity).expanduser().is_file():
        raise ValueError("The local SSH private key is missing. Run Prepare-Key.cmd or choose an existing key.")
    if config.get("bind_address"):
        ipaddress.IPv4Address(config["bind_address"])
    for key in ("yaw_sign", "lateral_sign"):
        if type(config.get(key)) is not int or config[key] not in (-1, 1):
            raise ValueError("Turn and strafe signs must be 1 or -1.")
    port = config.get("http_port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("The local HTTP port must be between 1024 and 65535.")
    if deployed:
        if not re.fullmatch(r"[0-9a-f]{64}", config.get("release") or ""):
            raise ValueError("Run Setup-Pi.cmd before opening the live console.")
        if not re.fullmatch(SERIAL, config.get("uart_serial") or ""):
            raise ValueError("Run Setup-Pi.cmd to select and verify the UART3 adapter.")
    return config


def require_tool(name):
    path = shutil.which(name)
    if not path:
        raise ValueError(f"{name} not found. Enable the Windows OpenSSH Client optional feature.")
    return path


def prepare_key():
    public = KEY.with_suffix(".pub")
    if not KEY.exists() and not public.exists():
        KEY.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([require_tool("ssh-keygen"), "-t", "ed25519", "-N", "", "-f", str(KEY),
                        "-C", "r3-control-kit"], check=True)
    if not KEY.is_file() or not public.is_file():
        raise ValueError("The kit key pair is incomplete. Restore it or move the remaining file before generating another.")
    print(f"Public key for the Pi: {public}")
    print("Keep the matching private key on this computer.")


def ssh(config, remote, *, trust=False, data=None, capture=False):
    argv = [require_tool("ssh"), "-T", "-F", "none", "-o", "ConnectTimeout=8",
            "-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
            "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "PasswordAuthentication=no", "-o", f"StrictHostKeyChecking={'ask' if trust else 'yes'}",
            "-o", f"BatchMode={'no' if trust else 'yes'}",
            "-i", str(Path(config["identity"]).expanduser().resolve())]
    if config.get("bind_address"):
        argv.extend(["-b", config["bind_address"]])
    proc = subprocess.run([*argv, config["host"], shlex.join(remote)], input=data,
                          stdout=subprocess.PIPE if capture else None, check=True)
    return proc.stdout.decode("utf-8") if capture else None


def pi_payload(root=ROOT):
    wheel = (root / "deps" / WHEEL).read_bytes()
    if sha256(wheel).hexdigest() != WHEEL_SHA256:
        raise ValueError("The bundled pySerial dependency did not pass its checksum.")
    result = io.BytesIO()
    with ZipFile(io.BytesIO(wheel)) as dependency, ZipFile(result, "w") as agent:
        def add(path, data):
            agent.writestr(ZipInfo(path), data, compress_type=ZIP_DEFLATED)

        for name in PI_FILES:
            add(name, (root / name).read_bytes())
        for name in sorted(dependency.namelist()):
            if name.startswith("serial/") and not name.endswith("/"):
                add(f".bench_deps/{name}", dependency.read(name))
        add("pyserial-LICENSE.txt", (root / "deps" / "pyserial-LICENSE.txt").read_bytes())
    return result.getvalue()


def pi_command(config, script, *args):
    return ["python3", "-B", f'r3-control-kit/releases/{config["release"]}/{script}', *args]


def read_only_check(config):
    ssh(config, pi_command(config, "r3_live.py", "--check", "--port", "auto",
                           "--uart-serial", config["uart_serial"]))


def read_config(path=CONFIG):
    if not path.is_file():
        raise ValueError("No local setup found. Run Setup-Pi.cmd first.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid local configuration. Run Setup-Pi.cmd again.")
    return value


def setup():
    old = read_config() if CONFIG.is_file() else {}

    def ask(label, default=""):
        return input(f"{label}{f' [{default}]' if default else ''}: ").strip() or default

    config = {
        "host": ask("Pi login (username@hostname.local or username@IPv4)", old.get("host", "r3@r3-pi.local")),
        "identity": ask("SSH private key", old.get("identity", str(KEY))).strip('"'),
        "bind_address": ask("PC IPv4 to bind SSH (blank for automatic, '-' to clear)", old.get("bind_address", "")),
        "yaw_sign": int(ask("Turn sign", str(old.get("yaw_sign", 1)))),
        "lateral_sign": int(ask("Strafe sign", str(old.get("lateral_sign", 1)))),
        "http_port": int(ask("Local web port", str(old.get("http_port", DEFAULT_PORT)))),
    }
    if config["bind_address"] == "-":
        config["bind_address"] = ""
    validate(config, deployed=False)
    data = pi_payload()
    config["release"] = sha256(data).hexdigest()
    print("For first contact, compare the SSH host fingerprint with the Pi's own screen before accepting it.")
    ssh(config, ["python3", "--version"], trust=True)
    ssh(config, ["python3", "-c", INSTALLER, config["release"]], data=data)
    print("Keep Motor OFF. Power the board in Mec mode and connect its UART3 USB data cable.")
    input("Press Enter when ready for the read-only check. ")
    ports = json.loads(ssh(config, pi_command(config, "r3_live.py", "--list-uarts"), capture=True))
    if not ports:
        raise ValueError("No matching UART3 USB adapter detected. Check the data cable and board power.")
    for i, port in enumerate(ports, 1):
        print(f'{i}. {port["device"]}  serial {port["serial"]}')
    selected = int(ask("Select the C30D UART3 adapter", "1" if len(ports) == 1 else ""))
    if not 1 <= selected <= len(ports):
        raise ValueError("Select one of the listed adapters.")
    config["uart_serial"] = ports[selected - 1]["serial"]
    if sum(port["serial"] == config["uart_serial"] for port in ports) != 1:
        raise ValueError("More than one adapter has that serial. Disconnect the duplicate and retry.")
    validate(config)
    read_only_check(config)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG)
    print("Setup complete. Double-click Start-Control.cmd.")


def start_args(config, *, simulation=False):
    args = [sys.executable, "-B", str(ROOT / "r3_ui.py"), "--http-port",
            str(DEFAULT_PORT if simulation else config["http_port"]), "--open-browser"]
    if not simulation:
        args += ["--host", config["host"], "--identity-file", config["identity"],
                 "--yaw-sign", str(config["yaw_sign"]), "--lateral-sign", str(config["lateral_sign"]),
                 "--remote-agent", pi_command(config, "r3_remote.py")[2],
                 "--uart-serial", config["uart_serial"]]
        if config.get("bind_address"):
            args += ["--bind-address", config["bind_address"]]
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("key", "setup", "check", "start", "simulation"))
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.exit(1, "Python 3.10 or newer is required.\n")
    try:
        if args.action == "key":
            prepare_key()
        elif args.action == "setup":
            setup()
        elif args.action == "simulation":
            subprocess.run(start_args({}, simulation=True), check=True)
        else:
            config = validate(read_config())
            if args.action == "check":
                read_only_check(config)
            else:
                require_tool("ssh")
                subprocess.run(start_args(config), check=True)
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, OSError, EOFError, subprocess.CalledProcessError) as error:
        print(f"R3 setup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
