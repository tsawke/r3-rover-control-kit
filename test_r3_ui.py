import http.client
import json
from http.server import ThreadingHTTPServer
import sys
from threading import Thread
import time
import unittest

from r3_control import JOG_VERSION
from r3_ui import Console, Handler, Session, TAB_LEASE, command_words


OWNER = "a" * 32


def until(condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.025)
    raise AssertionError("Timed out waiting for console state")


class ConsoleTests(unittest.TestCase):
    def test_only_finite_numeric_commands_and_expected_verbs(self):
        self.assertEqual(command_words({"action": "turn", "degrees": -10})[0], "turn -10.0000")
        self.assertEqual(command_words({"action": "goto", "x": 0, "y": 0.2})[0], "goto 0.0000 0.2000")
        for data in ({"action": "turn", "degrees": True}, {"action": "turn", "degrees": 361},
                     {"action": "goto", "x": float("nan"), "y": 0}, {"action": "raw"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                command_words(data)

    def test_live_requires_confirmation(self):
        console = Console(["must-not-be-spawned"])
        for ack in (None, False, "true"):
            with self.assertRaisesRegex(ValueError, "not been confirmed"):
                console.operate("connect", {"owner": OWNER, "mode": "live", "ack": ack})
        self.assertIsNone(console.session)

    def test_speed_limits_in_packets_are_range_checked(self):
        self.assertEqual(command_words({"action": "turn", "degrees": 90, "speed_deg_s": 9})[0],
                         "turn 90.0000 9.00")
        self.assertEqual(command_words({"action": "goto", "x": 0.3, "y": 0, "speed_mm_s": 65})[0],
                         "goto 0.3000 0.0000 65.00")
        for cmd in ({"action": "turn", "degrees": 90, "speed_deg_s": 14},
                    {"action": "goto", "x": 0.3, "y": 0, "speed_mm_s": False}):
            with self.assertRaisesRegex(ValueError, "Invalid speed"):
                command_words(cmd)

    def test_legacy_session_rejects_a_speed_it_cannot_honor(self):
        child = "import sys; print('{\"state\":\"ready\"}', flush=True); sys.stdin.buffer.read()"
        session = Session([sys.executable, "-u", "-c", child], OWNER, "sim")
        self.addCleanup(session.stop)
        until(lambda: session.snapshot()["status"] == "ready")
        self.assertFalse(session.snapshot()["speed_limits"])
        with self.assertRaisesRegex(ValueError, "does not support"):
            session.submit({"action": "turn", "degrees": 90, "speed_deg_s": 9})
        with self.assertRaisesRegex(ValueError, "does not support jog"):
            session.jog({"session": session.jog_session, "gesture": 1, "phase": "start"})

    def test_jog_reports_controller_fault_instead_of_missing_capability(self):
        fault = "Session ended: Board reports 10.42 V (expected 10.55-13.00 V)."
        ready = json.dumps({"state": "ready", "jog": JOG_VERSION})
        child = (
            f"import sys; print({ready!r}, flush=True); "
            "next(line for line in sys.stdin if line.startswith('>jog ')); "
            f"print({fault!r}, file=sys.stderr, flush=True); sys.exit(1)"
        )
        session = Session([sys.executable, "-u", "-c", child], OWNER, "sim")
        self.addCleanup(session.stop)
        until(lambda: session.snapshot()["status"] == "ready")
        gesture = {"session": session.snapshot()["jog"], "gesture": 1}
        session.jog({**gesture, "phase": "start", "direction": "forward", "speed": 60})
        until(session.idle)
        self.assertEqual(session.snapshot()["error"], fault)
        with self.assertRaisesRegex(ValueError, "10\\.42 V"):
            session.jog({**gesture, "phase": "hold"})
        session.jog({**gesture, "phase": "end"})
        self.assertEqual(session.snapshot()["error"], fault)

    def test_other_tab_cannot_send_commands_or_adopt_lease(self):
        console = Console()
        console.operate("connect", {"owner": OWNER, "mode": "sim"})
        self.addCleanup(console.shutdown)
        other = {"owner": "b" * 32}
        self.assertEqual(console.operate("status", other)["status"], "occupied")
        with self.assertRaisesRegex(ValueError, "does not own"):
            console.operate("command", {**other, "action": "turn", "degrees": 10})
        with self.assertRaisesRegex(ValueError, "already open"):
            console.operate("connect", {**other, "mode": "sim"})

    def start_sim(self):
        console = Console()
        console.operate("connect", {"owner": OWNER, "mode": "sim"})
        self.addCleanup(console.shutdown)
        until(lambda: console.session.snapshot()["status"] == "ready")
        return console

    def test_jog_cancel_before_start_and_stale_session_are_rejected(self):
        console = self.start_sim()
        jog = {"owner": OWNER, "session": console.session.snapshot()["jog"], "gesture": 1}
        console.operate("jog", {**jog, "phase": "end"})
        start = {**jog, "phase": "start", "direction": "forward", "speed": 80}
        with self.assertRaisesRegex(ValueError, "new press"):
            console.operate("jog", start)
        with self.assertRaisesRegex(ValueError, "different controller session"):
            console.operate("jog", {**start, "gesture": 2, "session": "old session"})
        self.assertEqual(console.session.snapshot()["status"], "ready")

    def test_jog_press_release_and_lost_lease_leave_the_same_origin(self):
        console = self.start_sim()
        get = console.session.snapshot
        jog = {"owner": OWNER, "session": get()["jog"], "gesture": 1}
        start = {**jog, "phase": "start", "direction": "forward", "speed": 80}
        for payload in ({"speed": 101}, {"speed": True}, {"direction": "unknown"}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "Invalid jog"):
                console.operate("jog", {**start, **payload})
        self.assertEqual(console.operate("jog", start)["status"], "moving")
        with self.assertRaisesRegex(ValueError, "not ready"):
            console.operate("command", {"owner": OWNER, "action": "turn", "degrees": 10})
        for _ in range(6):
            time.sleep(0.12)
            console.operate("jog", {**jog, "phase": "hold"})
        console.operate("jog", {**jog, "phase": "end"})
        until(lambda: get()["status"] == "ready")
        first = get()["pose"]
        self.assertGreater(first["x_m"], 0.04)
        self.assertEqual(first["y_m"], 0)
        console.operate("jog", {**start, "gesture": 2, "direction": "left"})
        until(lambda: get()["status"] == "ready", seconds=1.1)
        self.assertEqual(get()["pose"]["x_m"], first["x_m"])
        self.assertGreater(get()["pose"]["y_m"], 0)
        with self.assertRaisesRegex(ValueError, "expired"):
            console.operate("jog", {**jog, "phase": "hold", "gesture": 2})

    def test_stop_closes_child_stdin_while_moving(self):
        child = (
            "import sys; print('{\"state\":\"ready\"}', flush=True); "
            "sys.stdin.buffer.read(); sys.exit(0)"
        )
        session = Session([sys.executable, "-u", "-c", child], OWNER, "sim")
        self.addCleanup(session.stop)
        until(lambda: session.snapshot()["status"] == "ready")
        session.submit({"action": "turn", "degrees": 10})
        self.assertEqual(session.snapshot()["status"], "moving")
        session.stop()
        with self.assertRaisesRegex(ValueError, "not ready"):
            session.submit({"action": "turn", "degrees": 10})
        until(session.idle)
        self.assertEqual(session.child.returncode, 0)
        self.assertEqual(session.snapshot()["status"], "stopped")

    def test_browser_lease_expires(self):
        console = Console()
        console.operate("connect", {"owner": OWNER, "mode": "sim"})
        self.addCleanup(console.shutdown)
        until(console.session.idle, TAB_LEASE + 3.0)
        self.assertEqual(console.session.snapshot()["status"], "stopped")

    def test_http_checks_host_origin_and_secret(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.console = Console()
            server.host_header = f"127.0.0.1:{server.server_port}"
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = server.host_header
            conn = http.client.HTTPConnection(address, timeout=2)
            valid = {"Host": address, "Origin": "http://" + address,
                     "Content-Type": "application/json", "X-R3-Token": server.console.token}
            try:
                for key, value in (("Host", "outside.example"), ("Origin", "http://outside.example"),
                                   ("X-R3-Token", "no-token")):
                    conn.request("POST", "/api/status", json.dumps({"owner": OWNER}), {**valid, key: value})
                    with conn.getresponse() as response:
                        self.assertEqual(response.status, 403)
                        response.read()
                conn.request("POST", "/api/status", json.dumps({"owner": OWNER}), valid)
                with conn.getresponse() as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["status"], "vacant")
            finally:
                conn.close()
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
