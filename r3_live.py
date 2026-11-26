"""R3 tile-frame control over the adapted C30D UART3 firmware."""

import argparse
from dataclasses import replace
import json
import math
import shlex
import sys
from threading import Condition, Event, Thread
import time

from c30d_bench import BenchError, MOTION_HOLD_REASON, ZERO, open_port, quiet, uart_candidates
from c30d_protocol import TelemetryOdometry, TelemetryStream, encode_velocity
from r3_control import Controller, MotionError, body_velocity, execute, limit


MAX_FEEDBACK_AGE = 0.18
MAX_LINEAR = 0.10  # Sum of absolute body-frame components, m/s.
MAX_YAW = 0.40  # rad/s, below the firmware limit.
TURN_YAW_CAP = 0.23  # rad/s, limited separately from heading hold while translating.
TURN_COAST_TIME = 0.33  # s, estimated from the lifted stock-firmware stop trace.
TURN_TRIM_CAP = 0.045  # m/s, sum of absolute planar components during a turn.
MIN_START_VOLTAGE = 11.0
MIN_RUN_VOLTAGE = 10.55


def bounded_velocity(vx, vy, omega, *, turn=False):
    if not all(map(math.isfinite, (vx, vy, omega))):
        raise MotionError("Non-finite velocity.")
    linear = abs(vx) + abs(vy)
    if turn:
        # Trim is a correction, not a translation: don't apply the drive minimums.
        if not omega:
            return ZERO
        scale = min(1, TURN_TRIM_CAP / linear) if linear else 1
        return vx * scale, vy * scale, limit(omega, TURN_YAW_CAP)
    if linear:
        scale = min(MAX_LINEAR, max(linear, 0.055)) / linear
        vx, vy = vx * scale, vy * scale
    if omega:
        if omega:
            omega = math.copysign(max(abs(omega), 0.22), omega)
    return vx, vy, limit(omega, 0.25 if linear else MAX_YAW)


class SerialDrive:
    def __init__(self, port, *, yaw_sign=1, lateral_sign=1, clock=time.monotonic,
                 sleep=time.sleep, motion_permit=None):
        self.port, self.clock, self.sleep = port, clock, sleep
        self.yaw_sign, self.lateral_sign = yaw_sign, lateral_sign
        self.motion_permit = motion_permit
        self.decoder = TelemetryStream()
        self.odometry = TelemetryOdometry(yaw_sign=yaw_sign, max_gap=MAX_FEEDBACK_AGE)
        self.condition = Condition()
        self.done = Event()
        self.reader = Thread(target=self._read, name="c30d-feedback", daemon=True)
        self.sample = self.observation = self.error = None
        self.consumed_at = -math.inf
        self.armed = False
        self.drove = False
        self.motion_progress_at = None

    def __enter__(self):
        self.port.reset_input_buffer()
        self.reader.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.done.set()
        self.reader.join(timeout=0.4)

    def _ingest(self, data, now):
        batch = self.decoder.feed(data)
        if not batch:
            return
        with self.condition:
            previous = self.observation
            if len(batch) > 3 or (previous and now - previous.timestamp > MAX_FEEDBACK_AGE):
                raise MotionError("Delayed feedback; tile origin is no longer reliable.")
            # The board has no timestamps. Spread short USB bursts over the observed interval.
            first = previous.timestamp if previous else now - 0.05 * len(batch)
            period = (now - first) / len(batch)
            if len(batch) > 1 and period < 0.02:
                raise MotionError("Buffered feedback arrived too quickly to time reliably.")
            for index, sample in enumerate(batch):
                if (abs(sample.vx_m_s) + abs(sample.vy_m_s) > 0.20
                        or abs(sample.yaw_rate_wire_rad_s) > 0.75):
                    raise MotionError("Measured speed exceeds the supervised limit.")
                if self.armed:
                    self._check_board(sample, MIN_RUN_VOLTAGE)
                stationary = quiet(sample)
                corrected = replace(sample, vy_m_s=self.lateral_sign * sample.vy_m_s)
                if stationary:
                    corrected = replace(corrected, vx_m_s=0, vy_m_s=0, yaw_rate_wire_rad_s=0)
                at = now if index == len(batch) - 1 else first + period * (index + 1)
                self.observation = self.odometry.update(corrected, at)
                self.sample = sample
                if not stationary and self.motion_progress_at is not None:
                    self.motion_progress_at = max(self.motion_progress_at, at)
            self.condition.notify_all()

    def _read(self):
        try:
            while not self.done.is_set():
                pending = self.port.in_waiting
                if pending > 192:
                    raise MotionError("Serial backlog exceeded the feedback limit.")
                if pending:
                    self._ingest(self.port.read(pending), self.clock())
                self.done.wait(0.005)
        except Exception as error:
            with self.condition:
                self.error = MotionError(f"Feedback stopped: {error}")
                self.condition.notify_all()

    def _check_board(self, sample, min_voltage):
        if sample.stop_flag:
            raise MotionError(f"Board reports stop flag {sample.stop_flag}.")
        if not min_voltage <= sample.volts <= 13.0:
            raise MotionError(f"Board reports {sample.volts:.2f} V (expected {min_voltage:.2f}-13.00 V).")

    def _fresh(self):
        if self.error:
            raise self.error
        age = self.clock() - self.observation.timestamp if self.observation else math.inf
        if not 0 <= age <= MAX_FEEDBACK_AGE:
            raise MotionError("No fresh UART3 feedback; check the connection and tile origin.")
        return self.sample

    def observe(self):
        with self.condition:
            ready = self.condition.wait_for(lambda: self.error is not None or (
                self.observation is not None and self.observation.timestamp > self.consumed_at),
                timeout=MAX_FEEDBACK_AGE)
            if not ready:
                raise MotionError("No new UART3 feedback; check the connection and tile origin.")
            self._fresh()
            self.consumed_at = self.observation.timestamp
            return self.observation

    def at_rest(self):
        with self.condition:
            return quiet(self._fresh())

    def body_twist(self):
        with self.condition:
            sample = self._fresh()
            return (sample.vx_m_s, self.lateral_sign * sample.vy_m_s,
                    self.yaw_sign * sample.yaw_rate_wire_rad_s)

    def preflight(self, seconds=0.35, *, unlock=False):
        end = self.clock() + seconds
        while True:
            self.observe()
            with self.condition:
                sample = self._fresh()
                self._check_board(sample, MIN_START_VOLTAGE)
                if not quiet(sample):
                    raise MotionError("Vehicle must be stationary before starting a command.")
            if unlock:
                self.drove = True  # Ensure cleanup if even the first write fails.
                self._send(ZERO)
            if self.clock() >= end:
                return sample

    def arm(self):
        if self.motion_permit is not None:
            self.motion_permit()
        self.preflight(unlock=True)
        with self.condition:
            self.armed = True

    def _send(self, velocity):
        frame = encode_velocity(*velocity)
        if self.port.write(frame) != len(frame):
            raise MotionError("Incomplete UART3 write. Use the board's main power switch.")

    def stop(self):
        with self.condition:
            self.armed = self.drove = False
            self.motion_progress_at = None
        self._send(ZERO)
        self.sleep(0.05)
        self._send(ZERO)

    def command(self, vx, vy, omega):
        self._command(bounded_velocity(vx, vy, omega))

    def command_turn(self, vx, vy, omega):
        self._command(bounded_velocity(vx, vy, omega, turn=True))

    def _command(self, twist):
        vx, vy, omega = twist
        velocity = (vx, vy * self.lateral_sign, omega * self.yaw_sign)
        with self.condition:
            if velocity == ZERO:
                self.motion_progress_at = None
                if not self.drove:
                    return
            else:
                if not self.armed:
                    raise MotionError("Nonzero command requires a fresh preflight.")
                if self.motion_permit is not None:
                    self.motion_permit()
                sample = self._fresh()
                self._check_board(sample, MIN_RUN_VOLTAGE)
                now = self.clock()
                if self.motion_progress_at is None:
                    self.motion_progress_at = now
                elif now - self.motion_progress_at >= 2.0:
                    x, y, yaw = (round(1000 * value) for value in (
                        sample.vx_m_s, sample.vy_m_s, sample.yaw_rate_wire_rad_s))
                    raise MotionError("Command sent but no motion measured for 2 s "
                                      f"(vx={x} mm/s, vy={y} mm/s, yaw={yaw} mrad/s).")
        self._send(velocity)


class GroundController(Controller):
    max_age = MAX_FEEDBACK_AGE
    max_speed = MAX_LINEAR
    max_omega = MAX_YAW
    max_duration = 60.0
    yaw_tolerance = math.radians(1.25)

    def prepare_motion(self):
        self.turn_braking = False
        self.drive.arm()

    def at_rest(self):
        return self.drive.at_rest()

    def angular_command(self, error, *, in_place):
        if not in_place:
            return super().angular_command(error, in_place=False)
        direction = math.copysign(1.0, error)
        rate = direction * self.drive.body_twist()[2]
        if self.turn_braking and not self.at_rest():
            return 0.0
        # Once braking starts, coast to rest before allowing a reversal.
        predicted_error = abs(error) - max(0.0, rate) * TURN_COAST_TIME
        self.turn_braking = rate < -0.015 or predicted_error <= math.radians(0.30)
        if self.turn_braking:
            return 0.0
        return direction * min(TURN_YAW_CAP, 3.0 * predicted_error)

    def command_rotation(self, dx, dy, yaw, omega):
        if not omega:
            self.drive.command_turn(0.0, 0.0, 0.0)
            return
        mx, my, rate = self.drive.body_twist()
        if abs(rate) <= 0.015:
            self.drive.command_turn(0.0, 0.0, omega)
            return
        bx, by = body_velocity(dx, dy, yaw)
        self.drive.command_turn(1.8 * bx - 0.70 * mx, 1.8 * by - 0.70 * my, omega)


def parse_words(line):
    words = shlex.split(line)
    if len(words) in (1, 2):
        try:
            tuple(map(float, words))
        except ValueError:
            pass
        else:
            words.insert(0, "turn" if len(words) == 1 else "goto")
    return words


def run_session(drive, *, input_line=input, output=print):
    controller = GroundController(drive, clock=drive.clock, sleep=drive.sleep)
    output("Origin set for this session. Metres; +X ahead at start, +Y left, +angle CCW.")
    output("goto X Y (or X Y) | turn [degrees, default 10] | pose | stop | exit")
    try:
        while True:
            try:
                words = parse_words(input_line("r3> "))
                if not words:
                    continue
                if words == ["exit"]:
                    return
                if words == ["stop"]:
                    drive.stop()
                    p = controller.observe().pose
                else:
                    p = execute(controller, words)
                output(f"Encoder estimate: x={p.x:.3f} m  y={p.y:.3f} m  heading={math.degrees(p.yaw):.1f} deg")
            except ValueError as error:
                output(f"Rejected: {error}")
            finally:
                if drive.drove:
                    drive.stop()
    except EOFError:
        return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM6")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Read only; no writes to the board.")
    mode.add_argument("--list-uarts", action="store_true", help="List matching USB adapters without opening them.")
    mode.add_argument("--supervised-ground", action="store_true", help="On-site supervised motor control.")
    parser.add_argument("--yaw-sign", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--lateral-sign", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--uart-serial", help="Serial number of the verified UART3 adapter")
    args = parser.parse_args(argv)
    if args.supervised_ground and MOTION_HOLD_REASON:
        parser.exit(2, f"{MOTION_HOLD_REASON}\n")
    try:
        if args.list_uarts:
            print(json.dumps(uart_candidates()))
            return 0
        adapter = {"adapter_serial": args.uart_serial} if args.uart_serial else {}
        with open_port(args.port, **adapter) as port, SerialDrive(
            port, yaw_sign=args.yaw_sign, lateral_sign=args.lateral_sign,
        ) as drive:
            if args.check:
                drive.observe()
                drive.observe()
                with drive.condition:
                    sample = drive._fresh()
                print(f"UART3 verified: {sample.volts:.3f} V, stop_flag={sample.stop_flag}, "
                      f"stationary={quiet(sample)}; 0 command frames sent.")
            else:
                drive.preflight()
                run_session(drive)
        return 0
    except KeyboardInterrupt:
        return 130
    except (BenchError, ImportError, OSError, MotionError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
