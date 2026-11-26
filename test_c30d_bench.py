import contextlib
import io
import json
import struct
import unittest
from unittest.mock import patch

from c30d_bench import Bench, BenchError, FORWARD, ZERO, main
from c30d_protocol import checksum, decode_velocity


class ModelPort:
    def __init__(self, *, volts=12, sag=False, stop_delay=0.4):
        self.now = 0.0
        self.due = 0.0
        self.v = ZERO
        self.last_tx = -10.0
        self.sent = []
        self.volts, self.sag, self.stop_delay = volts, sag, stop_delay

    @property
    def in_waiting(self):
        return 24 if self.now >= self.due else 0

    def clock(self):
        return self.now

    def sleep(self, dt):
        self.now += dt

    def write(self, frame):
        self.v = decode_velocity(frame)
        self.last_tx = self.now
        self.sent.append((self.now, self.v))
        return len(frame)

    def read(self, size):
        self.due += 0.05
        rate = self.v[0] if self.now - self.last_tx < self.stop_delay else 0
        mv = 10300 if self.sag and rate else round(self.volts * 1000)
        body = struct.pack(">BB9hH", 0x7B, 0, round(rate * 1000), 0, 0, 0, 0, 0, 0, 0, 0, mv)
        return body + bytes((checksum(body), 0x7D))


class BenchTests(unittest.TestCase):
    def bench(self, port):
        self.trace = io.StringIO()
        return Bench(port, self.trace, clock=port.clock, sleep=port.sleep)

    def test_read_only_never_writes(self):
        port = ModelPort()
        self.assertGreaterEqual(len(self.bench(port).phase("read_only", 1.6)), 30)
        self.assertEqual(port.sent, [])

    def test_active_cli_is_blocked_before_opening_port_or_log(self):
        with (patch("c30d_bench.open_port") as open_port,
              patch("pathlib.Path.open") as open_log,
              contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised):
            main(["--log", "never-created.jsonl", "--timeout-trial", "--supervised-and-lifted"])
        self.assertEqual(raised.exception.code, 2)
        open_port.assert_not_called()
        open_log.assert_not_called()

    def test_timeout_trial_logs_rx_before_cleanup_zero(self):
        port = ModelPort()
        self.assertTrue(self.bench(port).timeout_trial())
        commands = [i for i, (_, v) in enumerate(port.sent) if v == FORWARD]
        actual_gap = port.sent[commands[-1] + 1][0] - port.sent[commands[-1]][0]
        self.assertGreaterEqual(actual_gap, 1.0)
        self.assertLess(port.sent[commands[-1]][0] - port.sent[commands[0]][0], 1.2)
        events = [json.loads(line) for line in self.trace.getvalue().splitlines()]
        result = next(i for i, event in enumerate(events) if event["event"] == "result")
        self.assertEqual(events[result]["visual_confirmation"], "pending")
        self.assertEqual(events[result + 1]["velocity"], list(ZERO))

    def test_low_voltage_preflight_sends_nothing(self):
        port = ModelPort(volts=10.8)
        with self.assertRaisesRegex(BenchError, "no frames"):
            self.bench(port).timeout_trial()
        self.assertFalse(port.sent)

    def test_sag_aborts_motion_and_sends_final_zero(self):
        port = ModelPort(sag=True)
        with self.assertRaisesRegex(BenchError, "canceled"):
            self.bench(port).timeout_trial()
        self.assertIn(FORWARD, [v for _, v in port.sent])
        self.assertEqual(port.sent[-1][1], ZERO)

    def test_no_silent_stop_does_not_pass(self):
        port = ModelPort(stop_delay=2.0)
        self.assertFalse(self.bench(port).timeout_trial())
        self.assertEqual(port.sent[-1][1], ZERO)


if __name__ == "__main__":
    unittest.main()
