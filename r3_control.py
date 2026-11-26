"""Tile-frame pose control. The executable only connects to a simulator."""

import argparse
from dataclasses import asdict, dataclass
import json
import math
import shlex
import time
from typing import Protocol


MOVE_SPEED_RANGE_MM_S = (55.0, 100.0)
TURN_SPEED_RANGE_DEG_S = (8.0, 13.18)
SPEED_LIMITS_VERSION = "motion-speed-v1"
JOG_VERSION = "hold-to-drive-v1"
JOG_AXES = {
    "forward": (1, 0, 0), "backward": (-1, 0, 0),
    "left": (0, 1, 0), "right": (0, -1, 0),
    "ccw": (0, 0, 1), "cw": (0, 0, -1),
}


def jog_velocity(direction, speed):
    if not isinstance(direction, str) or direction not in JOG_AXES:
        raise ValueError("Invalid jog direction.")
    x, y, yaw = JOG_AXES[direction]
    low, high = TURN_SPEED_RANGE_DEG_S if yaw else MOVE_SPEED_RANGE_MM_S
    if type(speed) not in (int, float) or not low <= speed <= high:
        raise ValueError("Invalid jog speed.")
    return x * speed / 1000, y * speed / 1000, yaw * math.radians(speed)


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    yaw: float  # Radians, positive counterclockwise.


@dataclass(frozen=True)
class Observation:
    pose: Pose
    timestamp: float  # Local monotonic seconds, at measurement acquisition.


class Drive(Protocol):
    def observe(self) -> Observation: ...
    def command(self, vx: float, vy: float, omega: float) -> None: ...


class MotionError(RuntimeError):
    pass


def angle_delta(delta: float) -> float:
    return math.remainder(delta, math.tau)


def limit(value: float, maximum: float) -> float:
    return max(-maximum, min(maximum, value))


def body_velocity(x: float, y: float, yaw: float) -> tuple[float, float]:
    c, s = math.cos(yaw), math.sin(yaw)
    return c * x + s * y, -s * x + c * y


def integrate_body(pose: Pose, dx: float, dy: float, d_yaw: float) -> Pose:
    """Integrate a constant body twist into a fixed ground frame."""
    half = d_yaw / 2
    scale = math.sin(half) / half if half else 1
    x, y = body_velocity(dx * scale, dy * scale, -(pose.yaw + half))
    return Pose(pose.x + x, pose.y + y, angle_delta(pose.yaw + d_yaw))


class Controller:
    # These are simulation starting values, not calibrated hardware settings.
    interval = 0.05
    max_age = 0.25
    settle_time = 0.30
    position_tolerance = 0.02
    yaw_tolerance = math.radians(0.25)
    max_speed = 0.12
    max_omega = math.radians(25)
    max_duration = 60.0
    max_step = 3.0

    def __init__(self, drive: Drive, *, clock=time.monotonic, sleep=time.sleep):
        self.drive = drive
        self.clock = clock
        self.sleep = sleep

    def observe(self) -> Observation:
        sample = self.drive.observe()
        p = sample.pose
        age = self.clock() - sample.timestamp
        if not all(map(math.isfinite, (p.x, p.y, p.yaw, age))) or not 0 <= age <= self.max_age:
            raise MotionError("Invalid or stale pose feedback.")
        return sample

    def move(self, x: float, y: float, speed_mm_s: float | None = None) -> Pose:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("Coordinates must be finite numbers.")
        if speed_mm_s is not None and not MOVE_SPEED_RANGE_MM_S[0] <= speed_mm_s <= MOVE_SPEED_RANGE_MM_S[1]:
            raise ValueError("Move speed limit must be 55 to 100 mm/s.")
        return self._run(destination=(x, y), speed_limit=speed_mm_s / 1000 if speed_mm_s is not None else None)

    def turn(self, degrees: float = 10.0, speed_deg_s: float | None = None) -> Pose:
        if not math.isfinite(degrees) or not -360 <= degrees <= 360:
            raise ValueError("Angle must be between -360 and 360 degrees.")
        if speed_deg_s is not None and not TURN_SPEED_RANGE_DEG_S[0] <= speed_deg_s <= TURN_SPEED_RANGE_DEG_S[1]:
            raise ValueError("Turn speed limit must be 8 to 13.18 deg/s.")
        return self._run(rotation=math.radians(degrees),
                         speed_limit=math.radians(speed_deg_s) if speed_deg_s is not None else None)

    def prepare_motion(self):
        """Optional transport preflight after validating the target."""

    def at_rest(self):
        return True

    def angular_command(self, error, *, in_place):
        return limit(2.2 * error, self.max_omega) if abs(error) > self.yaw_tolerance else 0.0

    def command_rotation(self, dx, dy, yaw, omega):
        self.drive.command(0.0, 0.0, omega)

    def _run(self, destination=None, rotation=0.0, speed_limit=None) -> Pose:
        started = self.clock()
        try:
            sample = self.observe()
            start = sample.pose
            goal_x, goal_y = destination if destination is not None else (start.x, start.y)
            if math.hypot(goal_x - start.x, goal_y - start.y) > self.max_step:
                raise ValueError("Target is more than 3 m from the current position.")
            self.prepare_motion()
            progress = 0.0
            settled_at = None
            previous = sample
            while True:
                now = self.clock()
                if now - started >= self.max_duration:
                    raise MotionError("Target did not settle before the timeout.")
                p = sample.pose
                progress += angle_delta(p.yaw - previous.pose.yaw)
                dx, dy = goal_x - p.x, goal_y - p.y
                distance = math.hypot(dx, dy)
                d_yaw = rotation - progress if destination is None else angle_delta(start.yaw - p.yaw)
                if destination is None and distance > self.position_tolerance:
                    raise MotionError(
                        f"Rotation drifted outside the position tolerance: {distance:.3f} m "
                        f"(dx={-dx:+.3f} m, dy={-dy:+.3f} m; turned {math.degrees(progress):+.1f} deg)."
                    )
                arrived = distance <= self.position_tolerance and abs(d_yaw) <= self.yaw_tolerance
                if arrived:
                    self.drive.command(0.0, 0.0, 0.0)
                    if self.at_rest():
                        settled_at = now if settled_at is None else settled_at
                        if now - settled_at >= self.settle_time:
                            return p
                    else:
                        settled_at = None
                else:
                    settled_at = None
                    vx, vy = 0.0, 0.0
                    if destination is not None and distance > self.position_tolerance:
                        gain = min(1.4, self.max_speed / distance)
                        vx, vy = body_velocity(gain * dx, gain * dy, p.yaw)
                        if speed_limit is not None:
                            scale = min(1.0, speed_limit / (abs(vx) + abs(vy)))
                            vx, vy = vx * scale, vy * scale
                    omega = self.angular_command(d_yaw, in_place=destination is None)
                    if destination is None:
                        if speed_limit is not None:
                            omega = limit(omega, speed_limit)
                        self.command_rotation(dx, dy, p.yaw, omega)
                    else:
                        self.drive.command(vx, vy, omega)
                self.sleep(self.interval)
                previous, sample = sample, self.observe()
                if sample.timestamp <= previous.timestamp:
                    raise MotionError("Pose feedback stopped updating.")
        finally:
            self.drive.command(0.0, 0.0, 0.0)


class Simulator:
    def __init__(self):
        self.pose = Pose(0.0, 0.0, 0.0)
        self.now = 0.0
        self.velocity = (0.0, 0.0, 0.0)

    def clock(self):
        return self.now

    def observe(self):
        return Observation(self.pose, self.now)

    def command(self, vx, vy, omega):
        self.velocity = vx, vy, omega

    def step(self, dt):
        vx, vy, omega = self.velocity
        self.pose = integrate_body(self.pose, vx * dt, vy * dt, omega * dt)
        self.now += dt


def execute(controller, words):
    verb, *args = words
    if verb == "goto" and len(args) in (2, 3):
        return controller.move(*map(float, args))
    if verb == "turn" and len(args) <= 2:
        return controller.turn(*map(float, args))
    if verb == "pose" and not args:
        return controller.observe().pose
    if verb == "stop" and not args:
        controller.drive.command(0.0, 0.0, 0.0)
        return controller.observe().pose
    raise ValueError("Use: goto X_m Y_m [mm/s] | turn [degrees [deg/s]] | pose | stop | exit")


def display(pose):
    record = asdict(pose)
    record["yaw_deg"] = round(math.degrees(record.pop("yaw")), 3)
    record["x"] = round(record["x"], 4)
    record["y"] = round(record["y"], 4)
    return json.dumps({"mode": "simulation", **record})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", action="store_true", required=True)
    parser.add_argument("--cmd", action="append", default=[], help="Command to run; repeat to retain the same origin.")
    args = parser.parse_args()
    sim = Simulator()
    controller = Controller(sim, clock=sim.clock, sleep=sim.step)
    try:
        if args.cmd:
            for command in args.cmd:
                print(display(execute(controller, shlex.split(command))))
            return 0
        print("SIMULATION ONLY | metres, CCW degrees | goto X Y | turn [degrees] | pose | stop | exit")
        while True:
            try:
                words = shlex.split(input("r3-sim> "))
                if not words:
                    continue
                if words == ["exit"]:
                    return 0
                print(display(execute(controller, words)))
            except (ValueError, MotionError) as error:
                print(f"Rejected: {error}")
    except EOFError:
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, MotionError) as error:
        parser.exit(1, f"Rejected: {error}\n")
    finally:
        sim.command(0.0, 0.0, 0.0)


if __name__ == "__main__":
    raise SystemExit(main())
