import math
import unittest

from odometry import WheelTravel
from r3_control import Pose


class OdometryTests(unittest.TestCase):
    dimensions = {"wheelbase_m": 0.20, "track_m": 0.16}

    def test_forward_from_encoder_counts(self):
        travel = WheelTravel.from_ticks([1560] * 4, diameter_m=0.075, counts_per_wheel_turn=1560)
        p = travel.advance(Pose(0, 0, 0), **self.dimensions)
        self.assertAlmostEqual(p.x, math.pi * 0.075)
        self.assertAlmostEqual(p.y, 0)
        self.assertAlmostEqual(p.yaw, 0)

    def test_strafe_is_relative_to_current_heading(self):
        p = WheelTravel(-0.5, 0.5, 0.5, -0.5).advance(Pose(1, 1, math.pi / 2), **self.dimensions)
        self.assertAlmostEqual(p.x, 0.5)
        self.assertAlmostEqual(p.y, 1)
        self.assertAlmostEqual(p.yaw, math.pi / 2)

    def test_in_place_rotation(self):
        d = math.radians(10) * 0.18
        p = WheelTravel(-d, d, -d, d).advance(Pose(1, 2, 0), **self.dimensions)
        self.assertAlmostEqual(p.x, 1)
        self.assertAlmostEqual(p.y, 2)
        self.assertAlmostEqual(math.degrees(p.yaw), 10)

    def test_body_velocity_and_turn_in_same_interval(self):
        d = math.pi / 2 * 0.18
        p = WheelTravel(1-d, 1+d, 1-d, 1+d).advance(Pose(0, 0, 0), **self.dimensions)
        self.assertAlmostEqual(p.x, 2 / math.pi)
        self.assertAlmostEqual(p.y, 2 / math.pi)
        self.assertAlmostEqual(p.yaw, math.pi / 2)

    def test_reject_unusable_parameters(self):
        with self.assertRaises(ValueError):
            WheelTravel.from_ticks([0] * 4, diameter_m=0.075, counts_per_wheel_turn=0)
        with self.assertRaises(ValueError):
            WheelTravel(0, 0, 0, 0).advance(Pose(0, 0, 0), wheelbase_m=0, track_m=0.16)


if __name__ == "__main__":
    unittest.main()
