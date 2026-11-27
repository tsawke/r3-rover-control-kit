from contextlib import contextmanager
from io import BytesIO, StringIO
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import unittest

from r3_ssh_password import host_fingerprint, paramiko_module, run_ssh


try:
    paramiko = paramiko_module()
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko is not None, "Optional password client is not installed")
class PasswordTransportTests(unittest.TestCase):
    @contextmanager
    def server(self, *, expect_refusal=False):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

        pem = Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
        key = paramiko.Ed25519Key.from_private_key(StringIO(pem.decode("ascii")))
        seen = {}
        executing = Event()
        errors = []

        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                seen["login"] = username, password
                return (paramiko.AUTH_SUCCESSFUL if seen["login"] == ("operator", "test-pass")
                        else paramiko.AUTH_FAILED)

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                seen["command"] = command
                executing.set()
                return True

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(4)

            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn, paramiko.Transport(conn) as transport:
                        transport.add_server_key(key)
                        transport.start_server(server=Server())
                        channel = transport.accept(3)
                        if channel is None:
                            return
                        with channel:
                            channel.settimeout(3)
                            if not executing.wait(3):
                                raise RuntimeError("Expected an exec request")
                            data = channel.makefile("rb").read()
                            channel.sendall(data[::-1])
                            channel.sendall_stderr(b"fixture stderr\n")
                            channel.send_exit_status(0)
                except (ConnectionError, EOFError) as error:
                    if not expect_refusal or "login" in seen:
                        errors.append(error)
                except Exception as error:
                    errors.append(error)

            worker = Thread(target=serve, daemon=True)
            worker.start()
            try:
                yield listener.getsockname()[1], host_fingerprint(key), seen
            finally:
                worker.join(5)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())

    def test_password_and_binary_io_against_ssh_server(self):
        with self.server() as (port, pin, seen):
            stdout, stderr = BytesIO(), BytesIO()
            data = bytes(range(256)) * 512
            code = run_ssh("operator@127.0.0.1", pin, "127.0.0.1", "test-pass",
                           ["python3", "verify.py", "two words"], BytesIO(data), stdout, stderr, port=port, timeout=5)
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), data[::-1])
            self.assertEqual(stderr.getvalue(), b"fixture stderr\n")
        self.assertEqual(seen["command"], b"python3 verify.py 'two words'")

    def test_host_mismatch_refuses_to_send_password(self):
        with self.server(expect_refusal=True) as (port, pin, seen):
            wrong = pin[:-1] + ("A" if pin[-1] != "A" else "B")
            with self.assertRaisesRegex(ValueError, "Pi host key"):
                run_ssh("operator@127.0.0.1", wrong, None, "test-pass",
                        ["python3", "-V"], BytesIO(), BytesIO(), BytesIO(), port=port, timeout=2)
        self.assertNotIn("login", seen)

    def test_first_contact_confirmation_is_required_before_login(self):
        with self.server(expect_refusal=True) as (port, pin, seen):
            def decline(fingerprint):
                self.assertEqual(fingerprint, pin)
                return False

            with self.assertRaisesRegex(ValueError, "not accepted"):
                run_ssh("operator@127.0.0.1", None, None, "test-pass",
                        ["python3", "-V"], BytesIO(), BytesIO(), BytesIO(), enroll=decline, port=port, timeout=2)
        self.assertNotIn("login", seen)

    def test_bridge_consumes_password_preamble_before_forwarding_input(self):
        with self.server() as (port, pin, seen):
            code = (f"from functools import partial; import r3_ssh_password as m; "
                    f"m.run_ssh = partial(m.run_ssh, port={port}); raise SystemExit(m.main())")
            pipe = json.dumps("test-pass").encode("ascii") + b"\n!\n>pose\n"
            argv = [sys.executable, "-B", "-c", code, "--host", "operator@127.0.0.1", "--fingerprint", pin,
                    "--", "python3", "-u", "r3_remote.py", "agent"]
            result = subprocess.run(argv, cwd=Path(__file__).parent, input=pipe, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"!\n>pose\n"[::-1])
            self.assertNotIn(b"test-pass", result.stderr)
