import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import control_kit as kit
from r3_remote import client_argv


class ControlKitTests(unittest.TestCase):
    def test_setup_saves_connection_only_after_read_only_check(self):
        with TemporaryDirectory() as temp:
            profile = Path(temp) / "profile.json"
            identity = Path(temp) / "dedicated identity"
            identity.touch()
            adapter = {"device": "/dev/ttyACM0", "serial": "NEW-ROBOT-123"}
            args = ["r3@r3-pi.local", str(identity), "192.168.1.4", "1", "1", "8770", "", ""]
            responses = [None, None, json.dumps([adapter]), None]
            with (patch.object(kit, "CONFIG", profile), patch.object(kit, "pi_payload", return_value=b"payload"),
                  patch.object(kit, "ssh", side_effect=responses) as ssh,
                  patch("builtins.input", side_effect=args), patch("builtins.print")):
                kit.setup()
            self.assertEqual(ssh.call_count, 4)
            self.assertTrue(ssh.call_args_list[0].kwargs["trust"])
            self.assertEqual(ssh.call_args_list[1].kwargs["data"], b"payload")
            self.assertEqual(ssh.call_args.args[1][-5:], ["--check", "--port", "auto", "--uart-serial", adapter["serial"]])
            config = kit.validate(kit.read_config(profile))
            self.assertEqual(config["release"], hashlib.sha256(b"payload").hexdigest())
            self.assertIn(adapter["serial"], kit.start_args(config))
            self.assertIn("192.168.1.4", kit.start_args(config))
            self.assertNotIn("--host", kit.start_args({}, simulation=True))

    def test_kit_agent_takes_serial_option_without_changing_legacy_path(self):
        args = SimpleNamespace(simulate=False, host="r3@r3-pi.local", port="auto", yaw_sign=1,
                               lateral_sign=1, identity_file=None, bind_address=None)
        self.assertIn("r3_pose_control/r3_remote.py", client_argv(args)[-1])
        args.remote_agent = "r3-control-kit/releases/" + "0" * 64 + "/r3_remote.py"
        args.uart_serial = "C30D-2"
        remote = client_argv(args)[-1]
        self.assertIn(args.remote_agent, remote)
        self.assertTrue(remote.endswith("--uart-serial C30D-2"))

    def test_installer_checks_checksum_and_preserves_existing_releases(self):
        archive = io.BytesIO()
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("r3_remote.py", "original\n")
        data = archive.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        with TemporaryDirectory() as temp:
            env = dict(os.environ, HOME=temp, USERPROFILE=temp)

            def install(payload):
                return subprocess.run([sys.executable, "-B", "-c", kit.INSTALLER, digest], input=payload,
                                      capture_output=True, env=env, timeout=5)

            script = Path(temp) / "r3-control-kit" / "releases" / digest / "r3_remote.py"
            self.assertNotEqual(install(b"damaged").returncode, 0)
            self.assertFalse(script.exists())
            self.assertEqual(install(data).returncode, 0)
            self.assertEqual(install(data).returncode, 0)
            script.write_text("operator's local edit", encoding="utf-8")
            self.assertNotEqual(install(data).returncode, 0)
            self.assertEqual(script.read_text(encoding="utf-8"), "operator's local edit")


if __name__ == "__main__":
    unittest.main()
