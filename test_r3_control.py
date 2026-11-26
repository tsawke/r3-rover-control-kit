import json
import math
from pathlib import Path
import subprocess
import sys
import unittest

from r3_control import Controller, MotionError, Observation, Pose, Simulator, angle_delta, execute


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.sim = Simulator()
        self.controller = Controller(self.sim, clock=self.sim.clock, sleep=self.sim.step)

    def assert_pose(self, p, x, y, yaw_deg):
        self.assertLessEqual(math.hypot(p.x - x, p.y - y), self.controller.position_tolerance)
        self.assertLessEqual(abs(angle_delta(p.yaw - math.radians(yaw_deg))), self.controller.yaw_tolerance)
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_default_turn(self):
        self.assert_pose(execute(self.controller, ["turn"]), 0, 0, 10)

    def test_custom_turn_right_through_wrap(self):
        self.sim.pose = Pose(0.4, 0.5, math.radians(-175))
        self.assert_pose(self.controller.turn(-15), 0.4, 0.5, 170)

    def test_full_turn_is_not_a_noop(self):
        self.assert_pose(self.controller.turn(360), 0, 0, 0)
        self.assertGreaterEqual(self.sim.now, 14.0)

    def test_full_turn_at_selected_low_rate(self):
        self.assert_pose(execute(self.controller, ["turn", "360", "8"]), 0, 0, 0)
        self.assertGreaterEqual(self.sim.now, 45.0)

    def test_invalid_speed_limits_are_rejected_before_moving(self):
        for words in (["goto", "1", "0", "101"], ["goto", "1", "0", "54"],
                      ["turn", "90", "14"], ["turn", "90", "nan"]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                execute(self.controller, words)
        self.assertEqual(self.sim.now, 0.0)

    def test_absolute_move_after_90_degree_turn(self):
        self.controller.turn(90)
        p = self.controller.move(0.6, -0.3)
        self.assert_pose(p, 0.6, -0.3, 90)
        self.assert_pose(self.controller.move(0.6, 0.2), 0.6, 0.2, 90)
        self.assert_pose(self.controller.move(0, 0), 0, 0, 90)

    def test_non_finite_commands_and_overlong_move_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.move(float("nan"), 0)
        with self.assertRaises(ValueError):
            self.controller.turn(float("inf"))
        with self.assertRaises(ValueError):
            self.controller.move(4, 0)
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_feedback_lost_while_moving(self):
        fresh = self.sim.observe
        self.sim.observe = lambda: fresh() if self.sim.now < 0.15 else Observation(self.sim.pose, 0.0)
        with self.assertRaises(MotionError):
            self.controller.move(1, 0)
        self.assertGreater(self.sim.pose.x, 0)
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_rotation_drift_stops(self):
        def drift(dt):
            self.sim.step(dt)
            self.sim.pose = Pose(0.04, 0, self.sim.pose.yaw)
        self.controller.sleep = drift
        with self.assertRaisesRegex(MotionError, "drifted"):
            self.controller.turn()
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_motion_timeout_stops(self):
        self.controller.sleep = lambda dt: setattr(self.sim, "now", self.sim.now + dt)
        self.controller.max_duration = 0.3
        with self.assertRaisesRegex(MotionError, "timeout"):
            self.controller.move(1, 0)
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_keyboard_interrupt_stops(self):
        def interrupt(_dt):
            raise KeyboardInterrupt
        self.controller.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.controller.move(1, 0)
        self.assertEqual(self.sim.velocity, (0, 0, 0))

    def test_cli_requires_sim_and_shares_pose_across_commands(self):
        script = str(Path(__file__).with_name("r3_control.py"))
        rejected = subprocess.run([sys.executable, script, "--cmd", "turn"], capture_output=True, timeout=5)
        self.assertNotEqual(rejected.returncode, 0)
        run = subprocess.run(
            [sys.executable, script, "--sim", "--cmd", "goto 0.6 0", "--cmd", "turn", "--cmd", "goto 0 0"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        a, b, c = map(json.loads, run.stdout.splitlines())
        self.assertAlmostEqual(a["x"], 0.6, delta=0.02)
        self.assertAlmostEqual(b["yaw_deg"], 10, delta=1.0)
        self.assertEqual(a["x"], b["x"])
        self.assertLess(math.hypot(c["x"], c["y"]), 0.02)
        self.assertEqual(c["mode"], "simulation")


if __name__ == "__main__":
    unittest.main()
