import io
import math
from pathlib import Path
import re
from types import SimpleNamespace
import subprocess
import sys
import unittest

from c30d_bench import ZERO
from r3_control import MotionError
from r3_live import GroundController
from r3_remote import CommandLink, HEARTBEAT_LIMIT, JOG_LEASE, client_argv, run_jog, serve
from test_r3_live import FakeBoard


class RemoteTests(unittest.TestCase):
    def test_dedicated_key_is_only_used_by_ssh_client(self):
        identity = Path("key with spaces")
        argv = client_argv(SimpleNamespace(simulate=True, host="r3lab@r3-pi.local", identity_file=identity,
                                           bind_address="192.168.1.10"))
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertEqual(argv[argv.index("-i") + 1], str(identity.resolve()))
        self.assertNotIn(identity.name, argv[-1])
        self.assertEqual(argv[argv.index("-b") + 1], "192.168.1.10")

    def test_terminal_client_to_agent_in_one_tile_frame(self):
        script = Path(__file__).with_name("r3_remote.py")
        result = subprocess.run(
            [sys.executable, str(script), "connect", "--simulate"],
            input="turn\n0.40 0.25\n-15\n0 0\nexit\n", text=True,
            capture_output=True, timeout=10, check=True,
        )
        moves = [line.split("simulated: ")[1] for line in result.stdout.splitlines()
                 if "simulated: " in line]
        self.assertEqual(len(moves), 4)
        poses = [tuple(map(float, re.findall(r"=(-?[0-9.]+)", line))) for line in moves]
        self.assertAlmostEqual(poses[0][2], 10, delta=0.26)
        self.assertAlmostEqual(poses[1][2], poses[0][2], delta=0.26)
        self.assertLessEqual(math.hypot(poses[1][0] - 0.40, poses[1][1] - 0.25), 0.021)
        self.assertAlmostEqual(poses[2][2] - poses[1][2], -15, delta=0.26)
        self.assertLessEqual(math.hypot(*poses[-1][:2]), 0.021)

    def test_missing_pc_heartbeat_aborts_live_motion_and_sends_zero(self):
        board = FakeBoard()
        link = CommandLink(clock=lambda: board.now)
        link.receive(b">0.40 0.25")
        board.drive.motion_permit = link.permit
        ctl = GroundController(board.drive, clock=board.drive.clock, sleep=board.advance)
        with self.assertRaisesRegex(MotionError, "PC link lost"):
            serve(link, ctl, lambda _: None)
        self.assertTrue(any(velocity != ZERO for velocity in board.sent))
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])
        self.assertLessEqual(board.now, HEARTBEAT_LIMIT + 0.25)
        with self.assertRaises(MotionError):
            link.receive(b"!")

    def test_expired_session_cannot_be_rearmed_by_late_heartbeat(self):
        now = [0.0]
        link = CommandLink(clock=lambda: now[0])
        now[0] = 0.5
        link.receive(b"!")
        now[0] = 1.0
        link.permit()
        now[0] += HEARTBEAT_LIMIT
        with self.assertRaises(MotionError):
            link.receive(b"!")

    def test_jog_watchdog_stops_even_with_pc_heartbeat_and_then_allows_new_press(self):
        board = FakeBoard()
        link = CommandLink(clock=lambda: board.now)
        board.drive.motion_permit = link.permit
        link.receive(b">jog left 60")

        def tick(seconds):
            board.advance(seconds)
            link.receive(b"!")

        ctl = GroundController(board.drive, clock=board.drive.clock, sleep=tick)
        pose = run_jog(link, ctl, link.next_command().split(), simulate=False)
        self.assertTrue(any(y == 0.060 and x == yaw == 0 for x, y, yaw in board.sent))
        self.assertGreater(pose.y, 0)
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])
        self.assertLessEqual(board.now, JOG_LEASE + 0.20)
        link.receive(b"+")
        self.assertFalse(link.jog_active())
        link.receive(b">jog right 60")
        self.assertTrue(link.jog_active())

    def test_jog_renewal_continues_motion_until_explicit_release(self):
        board = FakeBoard()
        link = CommandLink(clock=lambda: board.now)
        board.drive.motion_permit = link.permit
        link.receive(b">jog cw 9")

        def tick(seconds):
            board.advance(seconds)
            link.receive(b"!")
            if board.now >= 1.0:
                link.receive(b"-")
            link.receive(b"+")  # Late renewals cannot resurrect the released jog.

        ctl = GroundController(board.drive, clock=board.drive.clock, sleep=tick)
        run_jog(link, ctl, link.next_command().split(), simulate=False)
        self.assertEqual({v for v in board.sent if v != ZERO}, {(0, 0, -0.157)})
        self.assertGreaterEqual(board.now, 1.0)
        self.assertLess(board.now, 1.2)
        self.assertEqual(board.sent[-2:], [ZERO, ZERO])

    def test_release_during_preflight_never_sends_a_nonzero_frame(self):
        board = FakeBoard()
        link = CommandLink(clock=lambda: board.now)
        link.receive(b">jog forward 100")
        ctl = GroundController(board.drive, clock=board.drive.clock, sleep=board.advance)
        prepare = ctl.prepare_motion

        def release():
            prepare()
            link.receive(b"-")

        ctl.prepare_motion = release
        run_jog(link, ctl, link.next_command().split(), simulate=False)
        self.assertTrue(board.sent)
        self.assertTrue(all(velocity == ZERO for velocity in board.sent))

    def test_remote_jog_lease_does_not_revive_after_expiration(self):
        now = [0.0]
        link = CommandLink(clock=lambda: now[0])
        link.receive(b">jog forward 60")
        now[0] = JOG_LEASE
        link.receive(b"+")
        self.assertFalse(link.jog_active())

    def test_pipe_eof_invalidates_queued_command(self):
        link = CommandLink()
        link.read(io.BytesIO(b"!\n>turn\n").read)
        with self.assertRaises(MotionError):
            link.next_command()

    def test_overlong_frame_closes_link(self):
        link = CommandLink()
        link.read(io.BytesIO(b">" + b"x" * 256).read)
        with self.assertRaises(MotionError):
            link.permit()


if __name__ == "__main__":
    unittest.main()
