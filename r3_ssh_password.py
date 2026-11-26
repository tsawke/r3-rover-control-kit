"""Password-authenticated SSH transport for the optional Windows client mode."""

import argparse
from base64 import b64encode
from contextlib import closing
from getpass import GetPassWarning, getpass
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import shlex
import socket
import sys
from threading import Thread
import time
import warnings

from r3_remote import remote_words


FINGERPRINT = r"SHA256:[A-Za-z0-9+/]{43}"
CLIENT_DEPS = Path(__file__).resolve().parent / ".client_deps"


def paramiko_module():
    if CLIENT_DEPS.is_dir() and str(CLIENT_DEPS) not in sys.path:
        sys.path.insert(0, str(CLIENT_DEPS))
    import paramiko
    return paramiko


def prompt_password():
    if not sys.stdin.isatty():
        raise ValueError("Enter the Pi password in a local terminal.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("Pi account password: ")
    except GetPassWarning as error:
        raise ValueError("This terminal cannot hide password entry.") from error
    if not password or len(password.encode("utf-8")) > 512:
        raise ValueError("Enter a valid, non-empty Pi account password.")
    return password


def host_fingerprint(key):
    return "SHA256:" + b64encode(sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


def run_ssh(host, expected, bind, password, remote, stdin, stdout, stderr, *, enroll=None, timeout=None, port=22):
    paramiko = paramiko_module()
    user, address = host.split("@", 1)
    try:
        with socket.create_connection((address, port), timeout=8, source_address=(bind, 0) if bind else None) as sock:
            with closing(paramiko.Transport(sock)) as transport:
                transport.get_security_options().key_types = ("ssh-ed25519",)
                transport.auth_timeout = 8
                transport.start_client(timeout=8)
                actual = host_fingerprint(transport.get_remote_server_key())
                if expected is None:
                    if enroll is None or not enroll(actual):
                        raise ValueError("Pi host key was not accepted.")
                elif not hmac.compare_digest(actual, expected):
                    raise ValueError("Pi host key does not match the fingerprint saved during setup.")
                transport.auth_password(user, password, fallback=False)
                if not transport.is_authenticated():
                    raise ValueError("The Pi did not accept password-only authentication.")
                with closing(transport.open_session(timeout=8)) as channel:
                    channel.settimeout(3)
                    channel.exec_command(shlex.join(remote))
                    return pump(channel, stdin, stdout, stderr, timeout)
    except paramiko.BadAuthenticationType as error:
        raise ValueError("The Pi does not allow SSH password login. Enable it on the Pi or select key login.") from error
    except paramiko.AuthenticationException as error:
        raise ValueError("Pi rejected the username or password.") from error
    except (OSError, paramiko.SSHException) as error:
        raise ValueError(f"SSH connection failed: {error}") from error


def pump(channel, stdin, stdout, stderr, timeout):
    def send():
        try:
            read = getattr(stdin, "read1", stdin.read)
            while chunk := read(4096):
                channel.sendall(chunk)
            channel.shutdown_write()
        except (OSError, EOFError):
            channel.close()

    Thread(target=send, daemon=True).start()
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if channel.recv_ready():
            stdout.write(channel.recv(32768))
            stdout.flush()
        if channel.recv_stderr_ready():
            stderr.write(channel.recv_stderr(32768))
            stderr.flush()
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            return channel.recv_exit_status()
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError("Pi command timed out.")
        time.sleep(0.01)


def bridge_args(args):
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "--host", args.host,
            "--fingerprint", args.host_fingerprint]
    if args.bind_address:
        argv.extend(["--bind-address", str(args.bind_address)])
    return [*argv, "--", *remote_words(args)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--bind-address")
    parser.add_argument("remote", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(FINGERPRINT, args.fingerprint) or len(args.remote) < 2 or args.remote[0] != "--":
        parser.error("A verified host fingerprint and a remote command are required.")
    try:
        # Only the local parent writes this first line; it is never sent to the remote command.
        stdin = sys.stdin.buffer.raw
        password = json.loads(stdin.readline(4096))
        if not isinstance(password, str) or not password:
            raise ValueError("No password supplied by the local console.")
        result = run_ssh(args.host, args.fingerprint, args.bind_address, password, args.remote[1:],
                         stdin, sys.stdout.buffer, sys.stderr.buffer)
        return result if result >= 0 else 1
    except (ImportError, ValueError, EOFError) as error:
        print(error, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
