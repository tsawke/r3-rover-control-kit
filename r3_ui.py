"""Loopback-only browser console for the R3 SSH controller."""

import argparse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import subprocess
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace
import webbrowser

from r3_control import JOG_AXES, JOG_VERSION, MOVE_SPEED_RANGE_MM_S, SPEED_LIMITS_VERSION, TURN_SPEED_RANGE_DEG_S, jog_velocity
from r3_remote import DEFAULT_AGENT, HEARTBEAT_PERIOD, KIT_AGENT_PATTERN, client_argv
from r3_ssh_password import FINGERPRINT, bridge_args, paramiko_module, prompt_password


UI = Path(__file__).with_name("ui")
TAB_LEASE = 1.25
JOG_TAB_LEASE = 0.45
MAX_REQUEST = 2048
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/icons.svg": ("icons.svg", "image/svg+xml"),
}


def command_words(data):
    def number(key, low, high):
        value = data.get(key)
        if (type(value) not in (int, float) or not low <= value <= high
                or not math.isfinite(value)):
            raise ValueError(f"Invalid {key}.")
        return float(value)

    action = data.get("action")
    if action == "goto":
        x, y = number("x", -100, 100), number("y", -100, 100)
        wire, record = f"goto {x:.4f} {y:.4f}", {"action": "goto", "x": x, "y": y}
        if "speed_mm_s" in data:
            speed = number("speed_mm_s", *MOVE_SPEED_RANGE_MM_S)
            wire += f" {speed:.2f}"
            record["speed_mm_s"] = speed
        return wire, record
    if action == "turn":
        degrees = number("degrees", -360, 360)
        wire, record = f"turn {degrees:.4f}", {"action": "turn", "degrees": degrees}
        if "speed_deg_s" in data:
            speed = number("speed_deg_s", *TURN_SPEED_RANGE_DEG_S)
            wire += f" {speed:.2f}"
            record["speed_deg_s"] = speed
        return wire, record
    raise ValueError("Unknown command.")


class Session:
    def __init__(self, argv, owner, mode, *, preamble=b""):
        self.owner, self.mode = owner, mode
        self.lock, self.done = Lock(), Event()
        self.wake_writer = Event()
        self.queue = Queue(maxsize=1)
        self.logs = deque(maxlen=16)
        self.pose = self.current = None
        self.speed_limits = False
        self.jog_supported = False
        self.jog_session = secrets.token_hex(12)
        self.jog_id = 0
        self.jog_until = 0.0
        self.error = ""
        self.status = "connecting"
        self.touched = time.monotonic()
        self.deadline = self.touched + 15.0
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, bufsize=0, **kwargs)
        self.errors = Thread(target=self._read_errors, daemon=True)
        self.errors.start()
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()
        Thread(target=self._write_and_watch, args=(preamble,), daemon=True).start()

    def touch(self):
        with self.lock:
            self.touched = time.monotonic()

    def submit(self, data):
        wire, record = command_words(data)
        with self.lock:
            if self.status != "ready" or self.done.is_set():
                raise ValueError("Controller is not ready.")
            if not self.speed_limits and ("speed_mm_s" in record or "speed_deg_s" in record):
                raise ValueError("Controller does not support per-command speed limits.")
            self.deadline = time.monotonic() + 63.0
            self.current = record
            self.error = ""
            self.status = "moving"
            self.queue.put_nowait(b">" + wire.encode("ascii") + b"\n")

    def jog(self, data):
        gesture, phase = data.get("gesture"), data.get("phase")
        if type(gesture) is not int or not 0 < gesture <= 2**53 - 1:
            raise ValueError("Invalid jog gesture.")
        with self.lock:
            if data.get("session") != self.jog_session:
                raise ValueError("Jog belongs to a different controller session.")
            if not self.jog_supported:
                raise ValueError("Controller does not support jog in this session.")
            now = time.monotonic()
            if phase == "end":
                if gesture >= self.jog_id:
                    self.jog_id = gesture
                    self.jog_until = 0.0
                    self.wake_writer.set()
                return
            if self.done.is_set():
                raise ValueError(self.error or "Controller session ended.")
            if phase == "hold":
                if gesture != self.jog_id or now >= self.jog_until or self.current is None:
                    raise ValueError("Jog expired; press again.")
                self.jog_until = now + JOG_TAB_LEASE
                return
            if phase != "start":
                raise ValueError("Invalid jog phase.")
            if self.status != "ready" or gesture <= self.jog_id:
                raise ValueError("Jog cannot start without a new press and a ready controller.")
            direction, speed = data.get("direction"), data.get("speed")
            jog_velocity(direction, speed)
            unit_key = "speed_deg_s" if JOG_AXES[direction][2] else "speed_mm_s"
            self.jog_id, self.jog_until = gesture, now + JOG_TAB_LEASE
            self.deadline = now + 63.0
            self.current = {"action": "jog", "direction": direction, unit_key: speed}
            self.error = ""
            self.status = "moving"
            self.queue.put_nowait(f">jog {direction} {speed:.2f}\n".encode("ascii"))
            self.wake_writer.set()

    def stop(self):
        with self.lock:
            if self.status not in ("stopped", "fault"):
                self.status = "stopping"
            self.done.set()
            self.wake_writer.set()

    def idle(self):
        with self.lock:
            return self.status in ("stopped", "fault")

    def snapshot(self):
        with self.lock:
            return {"mode": self.mode, "status": self.status, "pose": self.pose,
                    "current": self.current, "error": self.error, "history": list(self.logs),
                    "speed_limits": self.speed_limits,
                    "jog": self.jog_session if self.jog_supported else None}

    def _read_errors(self):
        with self.child.stderr as pipe:
            for line in iter(lambda: pipe.readline(1024), b""):
                message = line.decode("utf-8", "replace").strip()
                if message:
                    with self.lock:
                        self.error = message

    def _read(self):
        try:
            with self.child.stdout as pipe:
                for line in iter(lambda: pipe.readline(4096), b""):
                    record = json.loads(line)
                    with self.lock:
                        if self.done.is_set():
                            break
                        if record["state"] == "ready" and self.status == "connecting":
                            self.pose = {"x_m": 0.0, "y_m": 0.0, "yaw_deg": 0.0}
                            self.speed_limits = record.get("speed_limits") == SPEED_LIMITS_VERSION
                            self.jog_supported = record.get("jog") == JOG_VERSION
                        elif record["state"] in ("estimate", "simulated") and self.status == "moving":
                            pose = {key: float(record[key]) for key in ("x_m", "y_m", "yaw_deg")}
                            if not all(map(math.isfinite, pose.values())):
                                raise ValueError("Invalid controller pose.")
                            self.pose = pose
                            self.logs.append({**self.current, "pose": self.pose, "time": time.strftime("%H:%M:%S")})
                        elif record["state"] == "rejected" and self.status == "moving":
                            self.error = str(record.get("error", "Command rejected."))[:512]
                        else:
                            raise ValueError("Unexpected controller response.")
                        self.current = None
                        self.jog_until = 0.0
                        self.deadline = None
                        self.status = "ready"
        except (ValueError, KeyError, OSError, TypeError) as error:
            with self.lock:
                self.error = str(error)
        finally:
            self.done.set()
            self.wake_writer.set()

    def _write_and_watch(self, preamble):
        try:
            with self.child.stdin as pipe:
                if preamble and pipe.write(preamble) != len(preamble):
                    raise OSError("Incomplete local SSH input.")
                while not self.done.is_set() and self.child.poll() is None:
                    self.wake_writer.clear()
                    with self.lock:
                        if self.done.is_set():
                            break
                        now = time.monotonic()
                        if now - self.touched > TAB_LEASE:
                            self.status = "stopping"
                            self.error = "Browser heartbeat lost."
                            break
                        if self.deadline is not None and now >= self.deadline:
                            self.error = "Controller response timed out."
                            break
                        pipe.write(b"!\n")
                        try:
                            packet = self.queue.get_nowait()
                        except Empty:
                            pass
                        else:
                            if not packet.startswith(b">jog ") or now < self.jog_until:
                                pipe.write(packet)
                            else:
                                self.current = self.deadline = None
                                self.status = "ready"
                        if self.current and self.current["action"] == "jog":
                            pipe.write(b"+\n" if now < self.jog_until else b"-\n")
                    self.wake_writer.wait(HEARTBEAT_PERIOD)
        except OSError:
            pass
        finally:
            self.done.set()
            try:
                self.child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
            self.errors.join(timeout=0.2)
            with self.lock:
                if self.status == "stopping":
                    self.status = "stopped"
                else:
                    self.status = "fault"
                    self.error = self.error or "Controller disconnected."
                self.current = None


class Console:
    def __init__(self, live_argv=None, *, live_preamble=b""):
        self.live_argv = live_argv
        self.live_preamble = live_preamble
        self.token = secrets.token_hex(24)
        self.lock = Lock()
        self.session = None

    def operate(self, action, data):
        owner = data.get("owner")
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise ValueError("Invalid browser session.")
        with self.lock:
            s = self.session
            if action == "status" and (not s or s.owner != owner):
                return {"status": "vacant" if not s or s.idle() else "occupied", "live": bool(self.live_argv)}
            if action == "connect":
                mode = data.get("mode")
                if s and not s.idle():
                    raise ValueError("A controller session is already open.")
                if mode == "live" and data.get("ack") is True and self.live_argv:
                    argv = self.live_argv
                elif mode == "sim":
                    argv = client_argv(SimpleNamespace(simulate=True, host=None))
                else:
                    raise ValueError("Physical setup has not been confirmed.")
                self.session = s = Session(argv, owner, mode, preamble=self.live_preamble if mode == "live" else b"")
            if not s or s.owner != owner:
                raise ValueError("This tab does not own the controller.")
            if action == "stop":
                s.stop()
            elif action == "command":
                s.submit(data)
            elif action == "jog":
                s.jog(data)
            s.touch()
            return {**s.snapshot(), "live": bool(self.live_argv)}

    def shutdown(self):
        with self.lock:
            if self.session:
                self.session.stop()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _respond(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, data):
        self._respond(status, "application/json", json.dumps(data, ensure_ascii=True, allow_nan=False).encode())

    def do_GET(self):
        if self.headers.get("Host") != self.server.host_header:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Local console only."})
        asset = ASSETS.get(self.path)
        if not asset:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        name, content_type = asset
        payload = (UI / name).read_bytes()
        if self.path == "/":
            payload = payload.replace(b"__CONSOLE_TOKEN__", self.server.console.token.encode())
        self._respond(HTTPStatus.OK, content_type, payload)

    def do_POST(self):
        token = self.headers.get("X-R3-Token", "")
        if (self.headers.get("Host") != self.server.host_header
                or self.headers.get("Origin") != "http://" + self.server.host_header
                or not token.isascii() or not secrets.compare_digest(token, self.server.console.token)):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Local console only."})
        action = self.path.removeprefix("/api/")
        if not self.path.startswith("/api/") or action not in ("status", "connect", "command", "jog", "stop"):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid request.")
            data = json.loads(self.rfile.read(length))
            if type(data) is not dict:
                raise ValueError("Invalid request.")
            response = self.server.console.operate(action, data)
        except (ValueError, OSError) as error:
            return self._json(HTTPStatus.CONFLICT, {"error": str(error)[:512]})
        self._json(HTTPStatus.OK, response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--host", help="Trusted Pi SSH destination")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--identity-file", type=Path)
    auth.add_argument("--host-fingerprint", help="Use password authentication and verify this SHA-256 host fingerprint")
    parser.add_argument("--bind-address", type=ipaddress.ip_address)
    parser.add_argument("--yaw-sign", type=int, choices=(-1, 1))
    parser.add_argument("--lateral-sign", type=int, choices=(-1, 1))
    parser.add_argument("--remote-agent", default=DEFAULT_AGENT)
    parser.add_argument("--uart-serial", help="Serial number of the verified UART3 adapter")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    live = None
    preamble = b""
    if args.host:
        if (not re.fullmatch(r"[\w.][\w.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*", args.host)
                or args.yaw_sign is None or args.lateral_sign is None):
            parser.error("Live mode requires a valid Pi host and verified sign options.")
        if args.remote_agent != DEFAULT_AGENT and not re.fullmatch(KIT_AGENT_PATTERN, args.remote_agent):
            parser.error("Invalid Pi agent path.")
        if args.uart_serial is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", args.uart_serial):
            parser.error("Invalid UART3 serial number.")
        args.simulate, args.port = False, "auto"
        if args.host_fingerprint:
            if not re.fullmatch(FINGERPRINT, args.host_fingerprint):
                parser.error("Enter the Pi's SSH host fingerprint in SHA256 format.")
            try:
                paramiko_module()
                preamble = json.dumps(prompt_password(), ensure_ascii=False).encode("utf-8") + b"\n"
            except (ImportError, ValueError) as error:
                parser.error(f"{error} Run Setup-Pi.cmd first.")
            live = bridge_args(args)
        else:
            if not args.identity_file or not args.identity_file.expanduser().is_file():
                parser.error("Live mode requires a local SSH key or a verified host fingerprint for password login.")
            live = client_argv(args)
            live[1:1] = ["-F", "none", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    with ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler) as server:
        server.console = Console(live, live_preamble=preamble)
        server.host_header = f"127.0.0.1:{server.server_port}"
        print(f"R3 console: http://{server.host_header}/", flush=True)
        if args.open_browser:
            Thread(target=webbrowser.open, args=(f"http://{server.host_header}/",), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.console.shutdown()


if __name__ == "__main__":
    main()
