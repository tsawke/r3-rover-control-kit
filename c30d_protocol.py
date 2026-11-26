"""WHEELTEC R550/C30D UART3 wire format. This module never opens a port."""

from dataclasses import dataclass
from functools import reduce
import math
from operator import xor
import struct

from r3_control import MotionError, Observation, Pose, integrate_body

HEADER = 0x7B
TAIL = 0x7D
BAUD = 115200
COMMAND_SIZE = 11
TELEMETRY_SIZE = 24
_COMMAND = struct.Struct(">BBBhhhBB")
_TELEMETRY = struct.Struct(">BB9hHBB")
_DIAGNOSTIC_MAGIC = b"\xd3R3\x01"
_DIAGNOSTIC = struct.Struct(">4sIHH16hBB")
DIAGNOSTIC_FLAGS = ("boot_ready", "motor_enable", "software_stop", "voltage_ok", "mec",
                    "expected_board", "feedback_fault", "armed", "output_active", "encoder_filter_set",
                    "stock_reference", "timer_outputs_enabled", "stock_fixed_mec")


def checksum(data):
    return reduce(xor, data, 0)


def _unit1000(value):
    if not math.isfinite(value) or not -32.768 <= value <= 32.767:
        raise ValueError("Velocity must be finite and encodable as a signed 16-bit field.")
    return round(value * 1000)


def encode_velocity(vx, vy, yaw_rate_wire):
    """Encode normal motion in m/s and rad/s; yaw input uses the board's sign."""
    frame = _COMMAND.pack(HEADER, 0, 0, *map(_unit1000, (vx, vy, yaw_rate_wire)), 0, TAIL)
    return frame[:-2] + bytes((checksum(frame[:-2]), TAIL))


def decode_velocity(frame):
    """Decode a normal-motion command for offline tests and inspection."""
    _validate(frame, COMMAND_SIZE)
    _, mode, reserved, vx, vy, yaw_rate, _, _ = _COMMAND.unpack(frame)
    if mode != 0 or reserved != 0:
        raise ValueError("Not a normal-motion command.")
    return vx / 1000, vy / 1000, yaw_rate / 1000


def _validate(frame, size):
    if len(frame) != size or frame[0] != HEADER or frame[-1] != TAIL or checksum(frame[:-2]) != frame[-2]:
        raise ValueError("Invalid C30D frame length, markers, or checksum.")


@dataclass(frozen=True)
class Telemetry:
    stop_flag: int
    vx_m_s: float
    vy_m_s: float
    yaw_rate_wire_rad_s: float
    accel_raw: tuple[int, int, int]
    gyro_raw: tuple[int, int, int]
    volts: float


def decode_telemetry(frame):
    _validate(frame, TELEMETRY_SIZE)
    _, flag, vx, vy, yaw_rate, ax, ay, az, gx, gy, gz, voltage, _, _ = _TELEMETRY.unpack(frame)
    return Telemetry(flag, vx / 1000, vy / 1000, yaw_rate / 1000,
                     (ax, ay, az), (gx, gy, gz), voltage / 1000)


@dataclass(frozen=True)
class Diagnostics:
    board_ms: int
    flags: dict[str, bool]
    battery_mv: int
    raw_count: tuple[int, ...]
    measured_mm_s: tuple[int, ...]
    target_mm_s: tuple[int, ...]
    pwm_delta: tuple[int, ...]


def decode_diagnostic(frame):
    if (len(frame) != _DIAGNOSTIC.size or not frame.startswith(_DIAGNOSTIC_MAGIC)
            or frame[-1] != 0x3D or checksum(frame[:-2]) != frame[-2]):
        raise ValueError("Invalid R3 diagnostic packet.")
    _, tick, flags, mv, *values = _DIAGNOSTIC.unpack(frame)
    return Diagnostics(tick, {key: bool(flags & (1 << i)) for i, key in enumerate(DIAGNOSTIC_FLAGS)},
                       mv, tuple(values[:4]), tuple(values[4:8]), tuple(values[8:12]), tuple(values[12:16]))


class TelemetryStream:
    """Parse vendor speed reports and track the latest optional R3 diagnostic packet."""

    def __init__(self):
        self.pending = bytearray()
        self.rejected = 0
        self.latest_diagnostic = None
        self.diagnostic_count = 0

    def feed(self, data, *, on_diagnostic=None):
        self.pending.extend(data)
        frames = []
        offset = 0
        while True:
            normal = self.pending.find(bytes((HEADER,)), offset)
            diagnostic = self.pending.find(_DIAGNOSTIC_MAGIC[:1], offset)
            start = min((i for i in (normal, diagnostic) if i >= 0), default=-1)
            if start < 0:
                self.pending.clear()
                return frames
            size = TELEMETRY_SIZE if start == normal else _DIAGNOSTIC.size
            if start == diagnostic:
                prefix = self.pending[start:start + len(_DIAGNOSTIC_MAGIC)]
                if not _DIAGNOSTIC_MAGIC.startswith(prefix):
                    self.rejected += 1
                    offset = start + 1
                    continue
            if len(self.pending) - start < size:
                del self.pending[:start]
                return frames
            end = start + size
            try:
                if start == normal:
                    frames.append(decode_telemetry(self.pending[start:end]))
                else:
                    self.latest_diagnostic = decode_diagnostic(self.pending[start:end])
                    self.diagnostic_count += 1
                    if on_diagnostic is not None:
                        on_diagnostic(self.latest_diagnostic)
            except ValueError:
                self.rejected += 1
                offset = start + 1
            else:
                offset = end


class TelemetryOdometry:
    """Estimate pose from measured rates, not from the transmitted command."""

    def __init__(self, *, yaw_sign, max_gap=0.25):
        if yaw_sign not in (-1, 1):
            raise ValueError("Yaw sign must be +1 or -1.")
        if not (math.isfinite(max_gap) and max_gap > 0):
            raise ValueError("Maximum sample gap must be positive and finite.")
        self.yaw_sign = yaw_sign
        self.max_gap = max_gap
        self.pose = Pose(0, 0, 0)
        self.previous = None
        self.faulted = False

    def update(self, sample: Telemetry, timestamp: float) -> Observation:
        if self.faulted:
            raise MotionError("Odometry lost continuity; re-establish the origin.")
        if not all(map(math.isfinite, (timestamp, sample.vx_m_s, sample.vy_m_s, sample.yaw_rate_wire_rad_s))):
            self.faulted = True
            raise MotionError("Invalid feedback sample.")
        if self.previous is not None:
            previous, last_time = self.previous
            dt = timestamp - last_time
            if not 0 < dt <= self.max_gap:
                self.faulted = True
                raise MotionError("Feedback gap or out-of-order timestamp; re-establish the origin.")
            half_dt = dt / 2
            self.pose = integrate_body(self.pose,
                                       (previous.vx_m_s + sample.vx_m_s) * half_dt,
                                       (previous.vy_m_s + sample.vy_m_s) * half_dt,
                                       (previous.yaw_rate_wire_rad_s + sample.yaw_rate_wire_rad_s)
                                       * half_dt * self.yaw_sign)
        self.previous = sample, timestamp
        return Observation(self.pose, timestamp)
