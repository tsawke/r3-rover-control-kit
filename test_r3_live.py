import contextlib
import io
import math
import struct
import unittest
from unittest.mock import patch

from c30d_bench import ZERO
from c30d_protocol import checksum, decode_velocity
from r3_control import MotionError, Pose, angle_delta, integrate_body
from r3_live import (
    TURN_TRIM_CAP, TURN_YAW_CAP, GroundController, SerialDrive, bounded_velocity, main,
    parse_words, run_session,
)


def frame(velocity=ZERO, *, stop=0, volts=12):
    vx, vy, yaw = (round(value * 1000) for value in velocity)
    raw = struct.pack(">BB9hHBB", 0x7B, stop, vx, vy, yaw, 0, 0, 0, 0, 0, 0,
                      round(volts * 1000), 0, 0x7D)
    return raw[:-2] + bytes((checksum(raw[:-2]), 0x7D))


class FakeBoard:
    def __init__(self, **options):
        self.now = 0.0
        self.velocity = ZERO
        self.pose = Pose(0, 0, 0)
        self.sent = []
        self.volts = 12.0
        self.sag_on_motion = False
        self.moves = True
        self.drive = SerialDrive(self, clock=lambda: self.now, sleep=self.advance, **options)
        self.drive._ingest(frame(), self.now)
        observe = self.drive.observe

        def simulated_observe():
            if self.drive.consumed_at == self.now:
                self.advance(0.05)
            return observe()

        self.drive.observe = simulated_observe

    def write(self, packet):
        self.velocity = decode_velocity(packet)
        self.sent.append(self.velocity)
        return len(packet)

    def advance(self, seconds):
        self.now += seconds
        measured = self.measured_velocity(seconds)
        self.pose = integrate_body(self.pose, *(value * seconds for value in measured))
        volts = 10.50 if self.sag_on_motion and self.velocity != ZERO else self.volts
        self.drive._ingest(frame(measured, volts=volts), self.now)

    def measured_velocity(self, _seconds):
        return self.velocity if self.moves else ZERO

    def session(self, commands):
        next_line = iter(commands)
        output = []
        run_session(self.drive, input_line=lambda _: next(next_line), output=output.append)
        return output


class LaggedYawBoard(FakeBoard):
    def __init__(self):
        super().__init__()
        self.spin_time = 0.0
        self.rate = 0.0
        self.active_samples = []

    def measured_velocity(self, seconds):
        target = self.velocity[2]
        self.spin_time = self.spin_time + seconds if target else 0
        if self.spin_time < 0.85 and abs(self.rate) < 0.015:
            target = 0
        self.rate += (target - self.rate) * (1 - math.exp(-seconds / 0.30))
        self.active_samples.append((math.degrees(self.pose.yaw), self.velocity[2], self.rate))
        return 0, 0, self.rate


class CrossCoupledYawBoard(LaggedYawBoard):
    def __init__(self, x_per_yaw, y_per_yaw):
        super().__init__()
        self.x_per_yaw, self.y_per_yaw = x_per_yaw, y_per_yaw
        self.trim_x = self.trim_y = 0.0

    def measured_velocity(self, seconds):
        _, _, rate = super().measured_velocity(seconds)
        response = 1 - math.exp(-seconds / 0.24)
        self.trim_x += response * (self.velocity[0] - self.trim_x)
        self.trim_y += response * (self.velocity[1] - self.trim_y)
        return self.trim_x + rate * self.x_per_yaw, self.trim_y + rate * self.y_per_yaw, rate


class LiveTests(unittest.TestCase):
    def test_requested_speed_caps_reach_serial_frames(self):
        board = FakeBoard()
        ctl = GroundController(board.drive, clock=lambda: board.now, sleep=board.advance)
        ctl.move(0.30, 0.20, speed_mm_s=60)
        self.assertTrue(any(vx and vy for vx, vy, _ in board.sent))
        self.assertLessEqual(max(abs(x) + abs(y) for x, y, _ in board.sent), 0.061)
        board.drive.stop()
        board.sent.clear()
        result = ctl.turn(-90, speed_deg_s=9)
        self.assertAlmostEqual(math.degrees(result.yaw), -90, delta=1.25)
        self.assertLessEqual(max(abs(w) for _, _, w in board.sent), math.radians(9) + 0.001)
        self.assertEqual(board.sent[-1], ZERO)

    def test_closed_loop_at_default_angle_and_fixed_origin(self):
        board = FakeBoard()
        output = board.session(["turn", "pose", "0.40 0.26", "turn -15", "goto 0 0", "exit"])
        estimates = [line for line in output if line.startswith("Encoder estimate:")]
        self.assertEqual(len(estimates), 5)
        angles = [float(line.partition("heading=")[2].split()[0]) for line in estimates]
        self.assertAlmostEqual(angles[0], 10, delta=1.25)
        self.assertAlmostEqual(angles[2], angles[0], delta=1.25)
        self.assertAlmostEqual(angles[3] - angles[2], -15, delta=1.25)
        self.assertLess(math.hypot(board.pose.x, board.pose.y), 0.026)
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])
        first_motion = next(velocity for velocity in board.sent if velocity != ZERO)
        self.assertEqual(first_motion[:2], (0.0, 0.0))
        self.assertGreater(first_motion[2], 0)

    def test_turn_is_relative_across_angle_wraparound(self):
        board = FakeBoard()
        board.session(["turn 190", "exit"])
        self.assertAlmostEqual(angle_delta(board.pose.yaw - math.radians(190)), 0, delta=math.radians(1.6))
        self.assertEqual(board.pose.x, 0)
        self.assertEqual(board.pose.y, 0)

    def test_default_turn_brakes_early_with_startup_lag_and_coasting(self):
        board = LaggedYawBoard()
        output = board.session(["turn", "exit"])
        self.assertTrue(any(line.startswith("Encoder estimate:") for line in output))
        self.assertAlmostEqual(math.degrees(board.pose.yaw), 10, delta=1.25)
        self.assertLessEqual(max(abs(command[2]) for command in board.sent), TURN_YAW_CAP)
        self.assertTrue(any(0 < yaw < 8.0 and 0 < cmd < TURN_YAW_CAP for yaw, cmd, _ in board.active_samples))
        self.assertFalse(any(rate > 0.015 and cmd < 0 for _, cmd, rate in board.active_samples))
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])

    def test_turn_braking_latches_until_feedback_is_quiet(self):
        board = FakeBoard()
        ctl = GroundController(board.drive)
        ctl.prepare_motion()
        board.drive._ingest(frame((0, 0, 0.25)), board.now + 0.05)
        board.now += 0.05
        self.assertEqual(ctl.angular_command(math.radians(3), in_place=True), 0)
        board.drive._ingest(frame((0, 0, 0.08)), board.now + 0.05)
        board.now += 0.05
        self.assertEqual(ctl.angular_command(math.radians(-3), in_place=True), 0)
        board.drive._ingest(frame(), board.now + 0.05)
        board.now += 0.05
        self.assertLess(ctl.angular_command(math.radians(-3), in_place=True), 0)

    def test_turn_trim_counteracts_observed_scale_of_cross_axis_drift(self):
        for xb, yb in ((-0.07, -0.19), (0.02, -0.20)):
            with self.subTest(xb=xb, yb=yb):
                bare = CrossCoupledYawBoard(xb, yb)
                ctl = GroundController(bare.drive, clock=lambda: bare.now, sleep=bare.advance)
                ctl.command_rotation = lambda dx, dy, yaw, omega: bare.drive.command(0, 0, omega)
                with self.assertRaisesRegex(MotionError, "Rotation drifted"):
                    ctl.turn()

                board = CrossCoupledYawBoard(xb, yb)
                ctl = GroundController(board.drive, clock=lambda: board.now, sleep=board.advance)
                final = ctl.turn()
                self.assertLessEqual(math.hypot(final.x, final.y), 0.02)
                self.assertAlmostEqual(math.degrees(final.yaw), 10, delta=1.25)
                self.assertTrue(any(vy > 0 for _, vy, _ in board.sent))
                self.assertTrue(all(abs(x) + abs(y) <= TURN_TRIM_CAP + 0.001 for x, y, _ in board.sent))
                self.assertTrue(all(not (x or y) for x, y, w in board.sent if not w))
                self.assertEqual(board.sent[-1], ZERO)

    def test_turn_trim_cannot_override_the_drift_guard(self):
        board = CrossCoupledYawBoard(0.0, -0.60)
        ctl = GroundController(board.drive, clock=lambda: board.now, sleep=board.advance)
        with self.assertRaisesRegex(MotionError, "Rotation drifted"):
            ctl.turn()
        self.assertEqual(board.sent[-1], ZERO)

    def test_turn_trim_does_not_raise_low_axis_or_yaw_requests(self):
        self.assertEqual(bounded_velocity(0.005, -0.010, 0.08, turn=True), (0.005, -0.010, 0.08))
        self.assertEqual(bounded_velocity(0.005, -0.010, 0, turn=True), ZERO)
        x, y, w = bounded_velocity(1, -1, 1, turn=True)
        self.assertAlmostEqual(abs(x) + abs(y), TURN_TRIM_CAP)
        self.assertEqual(w, TURN_YAW_CAP)

    def test_invalid_target_and_pose_never_write(self):
        board = FakeBoard()
        output = board.session(["turn nan", "4 0", "pose", "exit"])
        self.assertEqual(sum(line.startswith("Rejected:") for line in output), 2)
        self.assertFalse(board.sent)
        self.assertEqual(parse_words("turn -15"), ["turn", "-15"])

    def test_voltage_sag_aborts_and_sends_zero(self):
        board = FakeBoard()
        board.sag_on_motion = True
        with self.assertRaisesRegex(MotionError, "10.50 V"):
            board.session(["goto 0.30 0"])
        self.assertTrue(any(velocity != ZERO for velocity in board.sent))
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])

    def test_no_motion_aborts_and_sends_zero(self):
        board = FakeBoard()
        board.moves = False
        with self.assertRaisesRegex(MotionError, "no motion measured"):
            board.session(["turn"])
        self.assertLess(board.now, 3)
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])

    def test_watchdog_counts_motion_in_earlier_frame_of_a_batch(self):
        board = FakeBoard()
        board.moves = False
        board.drive.arm()
        board.drive.command(0.08, 0, 0)
        for _ in range(19):
            board.advance(0.10)
            board.drive.command(0.08, 0, 0)
        board.now += 0.12
        board.drive._ingest(frame((0.01, 0, 0)) + frame(), board.now)
        self.assertTrue(board.drive.at_rest())  # The last sample alone would miss the motion.
        board.drive.command(0.08, 0, 0)
        for _ in range(18):
            board.advance(0.10)
            board.drive.command(0.08, 0, 0)
        board.advance(0.16)
        with self.assertRaisesRegex(MotionError, "no motion measured for 2 s"):
            board.drive.command(0.08, 0, 0)

    def test_missing_feedback_blocks_nonzero_after_arm(self):
        board = FakeBoard()
        board.drive.arm()
        board.now += 0.2
        with self.assertRaisesRegex(MotionError, "No fresh UART3"):
            board.drive.command(0.08, 0, 0)
        self.assertTrue(all(velocity == ZERO for velocity in board.sent))

    def test_stop_flag_during_motion_is_rejected(self):
        board = FakeBoard()
        board.drive.arm()
        with self.assertRaisesRegex(MotionError, "stop flag"):
            board.drive._ingest(frame(stop=1), board.now + 0.05)

    def test_batched_feedback_limit(self):
        board = FakeBoard()
        board.drive._ingest(frame((0.08, 0, 0)) * 2, 0.1)
        self.assertAlmostEqual(board.drive.observation.pose.x, 0.006)
        with self.assertRaisesRegex(MotionError, "Delayed feedback"):
            board.drive._ingest(frame(), 0.4)

    def test_board_signs_and_bounded_rates(self):
        board = FakeBoard(lateral_sign=-1, yaw_sign=-1)
        board.drive.arm()
        board.drive.command(3, 7, 6)
        vx, vy, yaw = board.sent[-1]
        self.assertAlmostEqual(abs(vx) + abs(vy), 0.1)
        self.assertLess(vy, 0)
        self.assertAlmostEqual(yaw, -0.25)
        board.advance(0.05)
        self.assertGreater(board.drive.observation.pose.y, 0)
        self.assertGreater(board.drive.observation.pose.yaw, 0)
        self.assertEqual(bounded_velocity(0, 0, 0), ZERO)
        self.assertEqual(bounded_velocity(0, 0, 2)[2], GroundController.max_omega)

    def test_read_only_cli_makes_no_writes(self):
        board = FakeBoard()
        stream = io.StringIO()
        with (patch("r3_live.open_port", return_value=contextlib.nullcontext(board)),
              patch("r3_live.SerialDrive", return_value=contextlib.nullcontext(board.drive)),
              contextlib.redirect_stdout(stream)):
            self.assertEqual(main(["--check"]), 0)
        self.assertFalse(board.sent)
        self.assertIn("0 command frames sent", stream.getvalue())

    def test_ground_cli_refuses_to_open_serial_after_invalid_encoder_evidence(self):
        with (patch("r3_live.open_port") as open_serial, contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as raised):
            main(["--supervised-ground"])
        self.assertEqual(raised.exception.code, 2)
        open_serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
