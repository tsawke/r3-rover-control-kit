"""Persistent tile-frame session over authenticated SSH; no extra network listener."""

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import shlex
import signal
import subprocess
import sys
from threading import Event, Lock, Thread
import time

from c30d_bench import BenchError, open_port
from r3_control import JOG_VERSION, SPEED_LIMITS_VERSION, Controller, MotionError, Simulator, execute, jog_velocity
from r3_live import GroundController, SerialDrive, parse_words


HEARTBEAT_PERIOD = 0.20
HEARTBEAT_LIMIT = 0.80
JOG_LEASE = 0.45
LINE_LIMIT = 128
DEFAULT_AGENT = "r3_pose_control/r3_remote.py"
KIT_AGENT_PATTERN = r"r3-control-kit/releases/[0-9a-f]{64}/r3_remote\.py"


class CommandLink:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last_beat = clock()
        self.closed = False
        self.jog_until = None
        self.lock = Lock()
        self.commands = Queue(maxsize=1)

    def _check(self):
        if self.closed or self.clock() - self.last_beat >= HEARTBEAT_LIMIT:
            self.closed = True
            raise MotionError("PC link lost; line of sight and a new origin are required to resume.")

    def permit(self):
        with self.lock:
            self._check()

    def receive(self, packet):
        with self.lock:
            self._check()
            if packet == b"!":
                self.last_beat = self.clock()
            elif packet == b"+":
                now = self.clock()
                if self.jog_until is not None and now < self.jog_until:
                    self.jog_until = now + JOG_LEASE
            elif packet == b"-":
                if self.jog_until is not None:
                    self.jog_until = -math.inf
            elif packet.startswith(b">"):
                command = packet[1:].decode("ascii")
                if command.startswith("jog "):
                    if self.jog_until is not None:
                        raise ValueError("Jog already pending.")
                    self.jog_until = self.clock() + JOG_LEASE
                self.commands.put_nowait(command)
            else:
                raise ValueError("Invalid link packet.")

    def jog_active(self):
        with self.lock:
            self._check()
            return self.jog_until is not None and self.clock() < self.jog_until

    def finish_jog(self):
        with self.lock:
            self.jog_until = None

    def read(self, read_bytes):
        buffer = b""
        try:
            while chunk := read_bytes(LINE_LIMIT):
                *lines, buffer = (buffer + chunk).split(b"\n")
                if len(buffer) > LINE_LIMIT or any(len(line) > LINE_LIMIT for line in lines):
                    raise ValueError("Link packet too long.")
                for line in lines:
                    self.receive(line)
        except (OSError, ValueError, MotionError, Full):
            pass
        finally:
            with self.lock:
                self.closed = True

    def next_command(self):
        while True:
            self.permit()
            try:
                return self.commands.get(timeout=0.05)
            except Empty:
                continue


def pose_record(p):
    return {"x_m": round(p.x, 4), "y_m": round(p.y, 4),
            "yaw_deg": round(math.degrees(p.yaw), 2)}


def run_jog(link, controller, words, *, simulate):
    drive = controller.drive
    try:
        if len(words) != 3:
            raise ValueError("Expected jog direction and speed.")
        vx, vy, yaw = jog_velocity(words[1], float(words[2]))
        if not link.jog_active():
            return controller.observe().pose
        started = link.clock()
        controller.prepare_motion()
        while link.jog_active() and link.clock() - started < controller.max_duration:
            if yaw and not simulate:
                drive.command_turn(0.0, 0.0, yaw)
            else:
                drive.command(vx, vy, yaw)
            if simulate:
                time.sleep(controller.interval)
            controller.sleep(controller.interval)
    finally:
        if simulate:
            drive.command(0.0, 0.0, 0.0)
        elif drive.drove:
            drive.stop()
        link.finish_jog()

    stop_by = controller.clock() + 1.0
    while True:
        link.permit()
        pose = controller.observe().pose
        if controller.at_rest():
            return pose
        if controller.clock() >= stop_by:
            raise MotionError("Could not confirm stop after jog. Use the board's main power switch.")


def serve(link, controller, emit, *, simulate=False):
    drive = controller.drive
    link.permit()
    emit({"state": "ready", "mode": "simulation" if simulate else "stock-supervised",
          "speed_limits": SPEED_LIMITS_VERSION, "jog": JOG_VERSION})
    while True:
        line = link.next_command()
        link.permit()
        try:
            words = parse_words(line)
            if words == ["exit"]:
                emit({"state": "closed"})
                return
            if words and words[0] == "jog":
                pose = run_jog(link, controller, words, simulate=simulate)
            elif not simulate and words == ["stop"]:
                drive.stop()
                pose = controller.observe().pose
            else:
                pose = execute(controller, words)
        except ValueError as error:
            emit({"state": "rejected", "error": str(error)})
            continue
        finally:
            if not simulate and drive.drove:
                drive.stop()
        link.permit()
        emit({"state": "estimate" if not simulate else "simulated", **pose_record(pose)})


def agent(args):
    def emit(record):
        print(json.dumps(record, ensure_ascii=True), flush=True)

    def interrupt(*_):
        raise KeyboardInterrupt

    # SSH may signal the remote shell on disconnect instead of closing stdin first.
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
    link = CommandLink()
    fd = sys.stdin.fileno()
    Thread(target=link.read, args=(lambda n: os.read(fd, n),), daemon=True).start()
    try:
        if args.simulate:
            sim = Simulator()
            command = sim.command

            def guarded(vx, vy, omega):
                if vx or vy or omega:
                    link.permit()
                command(vx, vy, omega)

            sim.command = guarded
            serve(link, Controller(sim, clock=sim.clock, sleep=sim.step), emit, simulate=True)
        else:
            adapter = {"adapter_serial": args.uart_serial} if args.uart_serial else {}
            with open_port(args.port, **adapter) as port, SerialDrive(
                    port, yaw_sign=args.yaw_sign, lateral_sign=args.lateral_sign,
                    motion_permit=link.permit) as drive:
                drive.preflight()
                serve(link, GroundController(drive), emit)
        return 0
    except KeyboardInterrupt:
        return 130
    except (BenchError, ImportError, MotionError, OSError) as error:
        print(f"Session ended: {error}", file=sys.stderr, flush=True)
        return 1


def client_argv(args):
    if args.simulate and not args.host:
        return [sys.executable, "-u", str(Path(__file__).resolve()), "agent", "--simulate"]
    remote = ["python3", "-u", getattr(args, "remote_agent", DEFAULT_AGENT), "agent"]
    if args.simulate:
        remote.append("--simulate")
    else:
        remote.extend(["--supervised-stock", "--port", args.port,
                       "--yaw-sign", str(args.yaw_sign), "--lateral-sign", str(args.lateral_sign)])
        if getattr(args, "uart_serial", None):
            remote.extend(["--uart-serial", args.uart_serial])
    ssh = ["ssh", "-T", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=2",
           "-o", "ServerAliveCountMax=2"]
    if args.identity_file:
        ssh.extend(["-o", "IdentitiesOnly=yes", "-i", str(args.identity_file.expanduser().resolve())])
    if args.bind_address:
        ssh.extend(["-b", str(args.bind_address)])
    return [*ssh, args.host, shlex.join(remote)]


def connect(args):
    proc = subprocess.Popen(client_argv(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    done, send_lock = Event(), Lock()

    def send(packet):
        with send_lock:
            if proc.stdin.write(packet + b"\n") != len(packet) + 1:
                raise OSError("Incomplete write to the session.")

    def heartbeat():
        try:
            while not done.is_set():
                send(b"!")
                done.wait(HEARTBEAT_PERIOD)
        except OSError:
            done.set()

    def reply():
        raw = proc.stdout.readline()
        if not raw:
            raise MotionError("No response from Pi; see the connection or UART3 error above.")
        return json.loads(raw)

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        ready = reply()
        if ready.get("state") != "ready":
            raise MotionError("Pi agent did not confirm readiness.")
        print(f"{ready['mode']} | X/Y in metres, +X forward at origin, +Y left, positive turn CCW")
        if not args.simulate:
            print("Stay at the main power switch. A Pi power loss or UART3 disconnect can defeat software stop.")
        print("goto X Y [mm/s] | turn [degrees [deg/s]] | degrees | pose | stop | exit | Ctrl+C to end session")
        while True:
            line = input("r3> ").strip()
            if not line:
                continue
            if not line.isascii() or len(line) >= LINE_LIMIT:
                print("Enter a short command such as: 0.20 0.10, turn, or -15")
                continue
            send(b">" + line.encode("ascii"))
            record = reply()
            if record["state"] == "closed":
                return 0
            if record["state"] == "rejected":
                print(f"Rejected: {record['error']}")
            else:
                print(f"{record['state']}: x={record['x_m']:.3f} m  y={record['y_m']:.3f} m  "
                      f"heading={record['yaw_deg']:.2f} deg")
    except (EOFError, KeyboardInterrupt):
        return 130
    finally:
        done.set()
        thread.join(timeout=0.3)
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("connect", "agent"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--supervised-stock", action="store_true")
    parser.add_argument("--host", help="Pi SSH destination, e.g. user@192.168.1.20")
    parser.add_argument("--identity-file", type=Path, help="Dedicated SSH private key on the PC")
    parser.add_argument("--bind-address", type=ipaddress.ip_address, help="PC address on the Pi's network")
    parser.add_argument("--port", default="auto", help="Verified UART3 device path or auto")
    parser.add_argument("--uart-serial", help="Serial number of the verified UART3 adapter")
    parser.add_argument("--yaw-sign", type=int, choices=(-1, 1))
    parser.add_argument("--lateral-sign", type=int, choices=(-1, 1))
    args = parser.parse_args(argv)
    if args.mode == "agent" and args.host is not None:
        parser.error("Only the client accepts --host.")
    if (args.identity_file or args.bind_address) and (args.mode != "connect" or not args.host):
        parser.error("--identity-file and --bind-address require connect --host.")
    if args.host is not None and not re.fullmatch(r"[\w.][\w.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*", args.host):
        parser.error("Use an SSH destination such as user@r3-pi.local or user@192.168.1.20.")
    if args.supervised_stock:
        if args.mode == "connect" and not args.host:
            parser.error("Stock operation requires --host.")
        if args.yaw_sign is None or args.lateral_sign is None:
            parser.error("Enter both yaw and lateral signs explicitly; verify them on the chassis.")
    try:
        return agent(args) if args.mode == "agent" else connect(args)
    except (MotionError, OSError, ValueError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
