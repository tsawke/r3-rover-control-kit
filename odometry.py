"""Ideal dead reckoning for a symmetric four-wheel X-pattern mecanum chassis."""

from dataclasses import dataclass
import math

from r3_control import Pose, integrate_body


@dataclass(frozen=True)
class WheelTravel:
    # Order by physical wheel location, not by motor connector label.
    front_left: float
    front_right: float
    rear_left: float
    rear_right: float

    @classmethod
    def from_ticks(cls, deltas, *, diameter_m, counts_per_wheel_turn):
        """Convert signed deltas in physical wheel order; forward rolling is positive."""
        ticks = tuple(deltas)
        if len(ticks) != 4 or not all(map(math.isfinite, ticks)):
            raise ValueError("Four finite tick deltas are required.")
        if not (math.isfinite(diameter_m) and diameter_m > 0):
            raise ValueError("Wheel diameter must be positive and finite.")
        if not (math.isfinite(counts_per_wheel_turn) and counts_per_wheel_turn > 0):
            raise ValueError("Counts per wheel revolution must be positive and finite.")
        scale = math.pi * diameter_m / counts_per_wheel_turn
        return cls(*(delta * scale for delta in ticks))

    def advance(self, pose: Pose, *, wheelbase_m: float, track_m: float) -> Pose:
        """Integrate body twist over one short encoder interval in the tile frame."""
        fl, fr, rl, rr = self.front_left, self.front_right, self.rear_left, self.rear_right
        if not all(map(math.isfinite, (fl, fr, rl, rr, pose.x, pose.y, pose.yaw))):
            raise ValueError("Pose and wheel travel must be finite.")
        if not all(math.isfinite(d) and d > 0 for d in (wheelbase_m, track_m)):
            raise ValueError("Wheelbase and track must be positive and finite.")
        lever = (wheelbase_m + track_m) / 2
        dx = (fl + fr + rl + rr) / 4
        dy = (-fl + fr + rl - rr) / 4
        turn = (-fl + fr - rl + rr) / (4 * lever)

        return integrate_body(pose, dx, dy, turn)
