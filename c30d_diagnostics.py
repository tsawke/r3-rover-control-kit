"""Receive-only R3 diagnostic capture; never sends a board command."""

import argparse
from dataclasses import asdict
import json
import math
import sys
import time

from c30d_bench import BenchError, open_port
from c30d_protocol import TelemetryStream


def capture(port, seconds, *, clock=time.monotonic, sleep=time.sleep):
    port.reset_input_buffer()
    decoder = TelemetryStream()
    stop_at = clock() + seconds
    standard = 0
    latest = None
    while clock() < stop_at:
        pending = port.in_waiting
        if pending > 4096:
            raise BenchError("Diagnostic receive backlog exceeded 4096 bytes.")
        if pending:
            frames = decoder.feed(port.read(pending))
            standard += len(frames)
            if frames:
                latest = frames[-1]
        sleep(0.005)
    return {
        "standard_frames": standard,
        "diagnostic_frames": decoder.diagnostic_count,
        "last_telemetry": asdict(latest) if latest else None,
        "last_diagnostic": asdict(decoder.latest_diagnostic) if decoder.latest_diagnostic else None,
        "rejected_candidates": decoder.rejected,
        "commands_sent": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0.2 <= args.seconds <= 15:
        parser.error("Capture duration must be between 0.2 and 15 seconds.")
    try:
        with open_port(args.port) as port:
            result = capture(port, args.seconds)
        print(json.dumps(result, indent=2))
        return 0 if result["diagnostic_frames"] else 1
    except (BenchError, ImportError, OSError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
