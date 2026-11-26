from contextlib import redirect_stderr
import io
import math
import struct
import unittest

from c30d_probe import listen, main as probe
from c30d_protocol import (
    TelemetryOdometry, TelemetryStream, checksum, decode_telemetry, decode_velocity,
    encode_velocity,
)
from r3_control import Controller, MotionError, Simulator, angle_delta

MANUAL_RX = bytes.fromhex("7B 00 00 9B 00 00 FF DF 00 60 00 0C 40 A8 FF FD 00 06 00 1E 5B 87 82 7D")
MANUAL_TX = bytes.fromhex("7B 00 00 00 64 00 00 00 00 1F 7D")


def report(vx=0, vy=0, yaw=0):
    data = struct.pack(">BB9hH", 0x7B, 0, *(round(v * 1000) for v in (vx, vy, yaw)),
                       0, 0, 16384, 0, 0, 0, 12000)
    return data + bytes((checksum(data), 0x7D))


class CodecTests(unittest.TestCase):
    def test_vendor_command_fixture_and_negative(self):
        self.assertEqual(encode_velocity(0.1, 0, 0), MANUAL_TX)
        self.assertEqual(decode_velocity(encode_velocity(-0.1, -0.05, 0.3)), (-0.1, -0.05, 0.3))
        for bad in (math.nan, math.inf, 32.768):
            with self.assertRaises(ValueError):
                encode_velocity(bad, 0, 0)

    def test_vendor_telemetry_fixture(self):
        result = decode_telemetry(MANUAL_RX)
        self.assertEqual(result.vx_m_s, 0.155)
        self.assertEqual(result.yaw_rate_wire_rad_s, -0.033)
        self.assertEqual(result.accel_raw, (96, 12, 16552))
        self.assertEqual(result.gyro_raw, (-3, 6, 30))
        self.assertEqual(result.volts, 23.431)

    def test_stream_recovers_from_extra_reports_and_corruption(self):
        parser = TelemetryStream()
        bad = bytearray(MANUAL_RX)
        bad[22] ^= 1
        self.assertEqual(parser.feed(b"\x7cgarbage\x7f" + bad + MANUAL_RX[:7]), [])
        self.assertEqual(parser.rejected, 1)
        # A valid packet may itself contain 0x7B at a data position.
        embedded_header = report(vx=0.123)
        decoded = parser.feed(MANUAL_RX[7:] + embedded_header)
        self.assertEqual(decoded, [decode_telemetry(MANUAL_RX), decode_telemetry(embedded_header)])
        self.assertFalse(parser.pending)

    def test_yaw_sign_and_gap_latch(self):
        sample = decode_telemetry(report(yaw=1))
        locator = TelemetryOdometry(yaw_sign=-1)
        locator.update(sample, 1.0)
        p = locator.update(sample, 1.1).pose
        self.assertAlmostEqual(p.yaw, -0.1)
        with self.assertRaises(MotionError):
            locator.update(sample, 2.0)
        with self.assertRaises(MotionError):
            locator.update(sample, 1.2)

    def test_probe_only_reads(self):
        class ReceiveOnly:
            in_waiting = len(MANUAL_RX)

            def read(self, _count):
                return MANUAL_RX

        clock = iter((0, 0.01, 0.06, 1.0)).__next__
        result = listen(ReceiveOnly(), 0.5, clock=clock)
        self.assertEqual(result["valid_frames"], 2)
        self.assertEqual(result["bytes_sent"], 0)
        self.assertEqual(result["latest"]["volts"], 23.431)

    def test_probe_requires_physical_disconnect_flag(self):
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
            probe(["--port", "COM999"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--motors-disconnected", stderr.getvalue())

    def test_closed_loop_over_encoded_commands_and_feedback(self):
        class WireSimulator:
            def __init__(self):
                self.sim = Simulator()
                self.decoder = TelemetryStream()
                self.locator = TelemetryOdometry(yaw_sign=1)
                self.latest = self.locator.update(decode_telemetry(report()), 0)

            def observe(self):
                return self.latest

            def command(self, vx, vy, omega):
                self.sim.command(*decode_velocity(encode_velocity(vx, vy, omega)))

            def step(self, dt):
                self.sim.step(dt)
                frame = self.decoder.feed(report(*self.sim.velocity))[0]
                self.latest = self.locator.update(frame, self.sim.now)

        drive = WireSimulator()
        controller = Controller(drive, clock=drive.sim.clock, sleep=drive.step)
        controller.turn(10)
        controller.move(0.35, -0.25)
        actual = drive.sim.pose
        self.assertLess(math.hypot(actual.x - 0.35, actual.y + 0.25), 0.03)
        self.assertLess(abs(angle_delta(actual.yaw - math.radians(10))), math.radians(1))
        self.assertEqual(drive.sim.velocity, (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
