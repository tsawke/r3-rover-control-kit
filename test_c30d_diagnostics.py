import struct
import io
import json
import unittest

from c30d_bench import Bench
from c30d_diagnostics import capture
from c30d_protocol import TelemetryStream, checksum


STANDARD = bytes.fromhex("7B 00 00 9B 00 00 FF DF 00 60 00 0C 40 A8 FF FD 00 06 00 1E 5B 87 82 7D")
BODY = (bytes.fromhex("D3 52 33 01 00 00 01 7B 01 BB 2D 8C")
        + struct.pack(">16h", 100, 105, -107, -102, 74, 78, 80, 76,
                      80, 80, 80, 80, 420, -430, -440, 450))
DIAGNOSTIC = BODY + bytes((checksum(BODY), 0x3D))


class DiagnosticsTests(unittest.TestCase):
    def test_split_and_interleaved_frames(self):
        stream = TelemetryStream()
        standard = []
        for byte in STANDARD + DIAGNOSTIC + STANDARD:
            standard.extend(stream.feed(bytes([byte])))
        self.assertEqual(len(standard), 2)
        self.assertEqual(stream.diagnostic_count, 1)
        diagnostic = stream.latest_diagnostic
        self.assertEqual(diagnostic.board_ms, 379)
        self.assertEqual(diagnostic.battery_mv, 11660)
        self.assertEqual(diagnostic.raw_count, (100, 105, -107, -102))
        self.assertEqual(diagnostic.measured_mm_s, (74, 78, 80, 76))
        self.assertEqual(diagnostic.pwm_delta, (420, -430, -440, 450))
        self.assertTrue(diagnostic.flags["output_active"])
        self.assertFalse(diagnostic.flags["feedback_fault"])

    def test_bad_diagnostic_recovers_normal_stream(self):
        stream = TelemetryStream()
        standard = stream.feed(b"\xd3?" + DIAGNOSTIC[:-2] + bytes((DIAGNOSTIC[-2] ^ 1, 0x3D)) + STANDARD)
        self.assertEqual(len(standard), 1)
        self.assertIsNone(stream.latest_diagnostic)

    def test_encoder_filter_configuration_flag(self):
        body = bytearray(BODY)
        body[8] |= 0x02
        stream = TelemetryStream()
        self.assertEqual(stream.feed(body + bytes((checksum(body), 0x3D))), [])
        self.assertTrue(stream.latest_diagnostic.flags["encoder_filter_set"])

    def test_stock_reference_flags_distinguish_fixed_model_and_timer_state(self):
        body = bytearray(BODY)
        body[8] |= 0x1C
        stream = TelemetryStream()
        stream.feed(body + bytes((checksum(body), 0x3D)))
        flags = stream.latest_diagnostic.flags
        self.assertTrue(all(flags[k] for k in ("stock_reference", "timer_outputs_enabled", "stock_fixed_mec")))

    def test_capture_reads_both_and_never_writes(self):
        class Port:
            data = STANDARD + DIAGNOSTIC
            now = 0.0

            @property
            def in_waiting(self):
                return len(self.data)

            def reset_input_buffer(self):
                pass

            def read(self, size):
                result, self.data = self.data[:size], self.data[size:]
                return result

            def sleep(self, dt):
                self.now += dt

        port = Port()  # No write method is needed.
        result = capture(port, 0.25, clock=lambda: port.now, sleep=port.sleep)
        self.assertEqual(result["commands_sent"], 0)
        self.assertEqual(result["standard_frames"], 1)
        self.assertEqual(result["diagnostic_frames"], 1)

        port = Port()
        log = io.StringIO()
        Bench(port, log, clock=lambda: port.now, sleep=port.sleep).phase("read_only", 0.25)
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        diagnostic, = [event for event in events if event["event"] == "diagnostic"]
        self.assertEqual(diagnostic["target_mm_s"], [80, 80, 80, 80])
        self.assertFalse(any(event["event"] == "tx" for event in events))


if __name__ == "__main__":
    unittest.main()
