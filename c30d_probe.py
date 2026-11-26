"""Listen to C30D UART3 telemetry. This tool does not transmit serial data."""

import argparse
from dataclasses import asdict
import json
import math
import time

from c30d_protocol import BAUD, TelemetryStream


def listen(port, seconds, *, clock=time.monotonic):
    parser = TelemetryStream()
    valid_count = count_bytes = 0
    latest = None
    deadline = clock() + seconds
    while clock() < deadline:
        data = port.read(max(1, min(port.in_waiting, 4096)))
        count_bytes += len(data)
        frames = parser.feed(data)
        if frames:
            latest = asdict(frames[-1])
            valid_count += len(frames)
    return {"bytes_received": count_bytes, "valid_frames": valid_count,
            "rejected_candidates": parser.rejected, "latest": latest, "bytes_sent": 0}


def main(argv=None):
    argp = argparse.ArgumentParser(description=__doc__)
    action = argp.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="List ports without opening any.")
    action.add_argument("--port", help="Select the confirmed UART3 port.")
    argp.add_argument("--seconds", type=float, default=3.0)
    argp.add_argument("--motors-disconnected", action="store_true",
                      help="Confirm that motor cables are physically disconnected.")
    args = argp.parse_args(argv)
    if args.port and not args.motors_disconnected:
        argp.error("Disconnect the motors and acknowledge with --motors-disconnected.")
    if not math.isfinite(args.seconds) or not 0.2 <= args.seconds <= 10:
        argp.error("--seconds must be between 0.2 and 10.")
    try:
        import serial
        from serial.tools.list_ports import comports
    except ImportError:
        argp.exit(2, "PySerial is required for port discovery/listening. Offline tests need no dependencies.\n")
    ports = comports()
    if args.list:
        for p in ports:
            print(json.dumps({"port": p.device, "description": p.description, "hardware_id": p.hwid}))
        return 0

    port = serial.Serial(baudrate=BAUD, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                         stopbits=serial.STOPBITS_ONE, timeout=0.05, xonxoff=False, rtscts=False, dsrdtr=False)
    port.port = args.port
    port.dtr = False
    port.rts = False
    try:
        port.open()
        result = listen(port, args.seconds)
    except OSError as exc:
        argp.exit(2, f"UART3 listen failed: {exc}\n")
    finally:
        port.close()
    print(json.dumps({"port": args.port, "baud": BAUD, **result}, indent=2))
    return 0 if result["valid_frames"] >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
