"""Read-only UART3 monitor and a single supervised, logged timeout trial."""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

from c30d_protocol import TelemetryStream, encode_velocity


UART3 = (0x1A86, 0x55D4, "5CF0014429")
ZERO = (0.0, 0.0, 0.0)
FORWARD = (0.08, 0.0, 0.0)
MOTION_HOLD_REASON = (
    "Encoder feedback and physical wheel motion have not been jointly verified. "
    "Powered motion is locked pending supervised validation; use a read-only check."
)


class BenchError(RuntimeError):
    pass


def quiet(sample):
    return (abs(sample.vx_m_s) <= 0.003 and abs(sample.vy_m_s) <= 0.003
            and abs(sample.yaw_rate_wire_rad_s) <= 0.015)


class Bench:
    def __init__(self, port, log, *, clock=time.monotonic, sleep=time.sleep):
        self.port, self.log, self.clock, self.sleep = port, log, clock, sleep
        self.origin = clock()
        self.decoder = TelemetryStream()
        self.last_rx = None
        self.last = None
        self.last_nonzero = None

    def emit(self, event, **fields):
        self.log.write(json.dumps({
            "host_ms": round((self.clock() - self.origin) * 1000, 2),
            "event": event, **fields,
        }, ensure_ascii=True) + "\n")

    def poll(self):
        pending = self.port.in_waiting
        if pending > 4096:
            raise BenchError("Serial backlog prevents useful time correlation.")
        frames = self.decoder.feed(self.port.read(pending), on_diagnostic=self.log_diagnostic) if pending else []
        for sample in frames:
            self.last_rx, self.last = self.clock(), sample
            self.emit("rx", **asdict(sample))
        return frames

    def log_diagnostic(self, sample):
        self.emit("diagnostic", **asdict(sample))

    def send(self, velocity):
        frame = encode_velocity(*velocity)
        if self.port.write(frame) != len(frame):
            raise BenchError("Incomplete UART write.")
        if velocity != ZERO:
            self.last_nonzero = self.clock()
        self.emit("tx", velocity=velocity, frame=frame.hex(" "))

    def phase(self, name, seconds, velocity=None):
        self.emit("phase", name=name, seconds=seconds)
        start = due = self.clock()
        samples = []
        while self.clock() - start < seconds:
            samples.extend(self.poll())
            now = self.clock()
            if velocity is not None and velocity != ZERO:
                m = self.last
                if (m is None or now - self.last_rx > 0.18 or m.stop_flag
                        or m.volts < 10.55 or not -0.025 <= m.vx_m_s <= 0.12
                        or abs(m.vy_m_s) > 0.085 or abs(m.yaw_rate_wire_rad_s) > 0.75):
                    raise BenchError("Pulse canceled: stale feedback, low voltage or speed limit.")
            if velocity is not None and now >= due and now - start < seconds:
                self.send(velocity)
                due = now + 0.05
            self.sleep(0.005)
        return samples

    def timeout_trial(self):
        base = self.phase("baseline", 0.65)
        if len(base) < 10 or any(f.volts < 11 or f.stop_flag or not quiet(f) for f in base):
            raise BenchError("Preflight failed; no frames sent.")
        try:
            settle = self.phase("zero_unlock", 0.45, ZERO)
            if not settle or any(f.volts < 11 or f.stop_flag or not quiet(f) for f in settle):
                raise BenchError("Unsafe pre-pulse state; no nonzero frames sent.")
            moving = self.phase("forward", 1.2, FORWARD)
            gap = self.phase("no_transmission", 1.0)
            observed = (any(not quiet(f) for f in moving) and len(gap) >= 12
                        and any(not quiet(f) for f in gap[:-5]) and all(map(quiet, gap[-5:])))
            self.emit("result", encoder_silent_stop=observed, gap_frames=len(gap),
                      visual_confirmation="pending", no_zero_in_gap=True)
            return observed
        finally:
            try:
                self.send(ZERO)
                self.sleep(0.05)
                self.send(ZERO)
            except Exception as error:
                self.emit("cleanup_error", message=str(error))
                raise BenchError("Final zero not confirmed sent. Use main power cutoff.") from error


def serial_modules():
    dependency_dir = Path(__file__).resolve().parent / ".bench_deps"
    if dependency_dir.is_dir() and str(dependency_dir) not in sys.path:
        sys.path.insert(0, str(dependency_dir))
    import serial
    from serial.tools import list_ports
    return serial, list_ports


def uart_candidates():
    _, list_ports = serial_modules()
    return [{"device": port.device, "serial": port.serial_number}
            for port in list_ports.comports()
            if (port.vid, port.pid) == UART3[:2] and port.serial_number]


def open_port(device, adapter_serial=None):
    serial, list_ports = serial_modules()
    expected = UART3 if adapter_serial is None else (*UART3[:2], adapter_serial)

    def matches(port):
        if (port.vid, port.pid, port.serial_number) != expected:
            return False
        if device == "auto":
            return True
        if os.name == "nt":
            return port.device.upper() == device.upper()
        return Path(port.device).resolve() == Path(device).resolve()

    match = [p for p in list_ports.comports() if matches(p)]
    if len(match) != 1:
        raise BenchError("Port is not the verified C30D UART3 adapter.")
    port = serial.Serial(baudrate=115200, timeout=0, write_timeout=0.1,
                         xonxoff=False, rtscts=False, dsrdtr=False,
                         exclusive=True if os.name != "nt" else None)
    port.dtr = port.rts = False
    port.port = match[0].device
    port.open()
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout-trial", action="store_true")
    parser.add_argument("--supervised-and-lifted", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_trial != args.supervised_and_lifted:
        parser.error("The powered trial requires both flags and on-site supervision.")
    if args.timeout_trial and MOTION_HOLD_REASON:
        parser.exit(2, f"{MOTION_HOLD_REASON}\n")
    try:
        with args.log.open("x", encoding="utf-8", buffering=1) as log, open_port(args.port) as port:
            bench = Bench(port, log)
            bench.emit("start", time=datetime.now().astimezone().isoformat(),
                       port=args.port, powered=args.timeout_trial)
            if args.timeout_trial:
                observed = bench.timeout_trial()
                print(f"Encoder stop during silence: {observed}; visual result still required.")
                return 0 if observed else 1
            frames = bench.phase("read_only", 1.6)
            print(f"Received {len(frames)} valid frames; 0 transmitted.")
            if frames:
                print(f"Board: {frames[-1].volts:.3f} V; quiet: {all(map(quiet, frames))}")
            return 0 if len(frames) >= 2 else 1
    except (ImportError, OSError, BenchError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
