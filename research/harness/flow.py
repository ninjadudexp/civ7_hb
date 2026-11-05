#!/usr/bin/env python3
"""flow.py - per-boot orchestrator for the Civ VII harness.

Runs the proven sequence so a boot is never lost to a stale accessor or a stale
payload:

  1. relocation payload: load -> mounts (writes harness/mount-accessor.txt) -> stop
  2. exit any stale harness payload holding the control port
  3. harness payload:    load
  4. harness <command>   (e.g. posttest, setup, wave abort-control)

Usage: python flow.py <console-ip> <command> [args...] [--port N]
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RELOC = os.path.join(HERE, "..", "relocation", "host", "relocate_host.py")
HOST = os.path.join(HERE, "host.py")


def run(args, env=None):
    print("$ " + " ".join(args), flush=True)
    subprocess.run([sys.executable] + args, env=env, check=False)


def exit_payload(host, port):
    """Best-effort: tell a stale payload to close its control socket."""
    try:
        sock = socket.create_connection((host, port), timeout=4)
    except OSError:
        return False
    try:
        body = json.dumps({"id": 1, "command": "exit"}).encode()
        sock.sendall(struct.pack("<I", len(body)) + body)
        sock.settimeout(3)
        try:
            sock.recv(4096)
        except OSError:
            pass
    finally:
        sock.close()
    for _ in range(20):
        time.sleep(0.1)
        probe = socket.socket()
        probe.settimeout(0.5)
        try:
            probe.connect((host, port))
            probe.close()
        except OSError:
            return True
        probe.close()
    return False


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    args = sys.argv[1:]
    port = 9029
    if "--port" in args:
        at = args.index("--port")
        port = int(args[at + 1])
        del args[at:at + 2]
    host, command = args[0], args[1]
    rest = args[2:]
    env = dict(os.environ, CIV_HARNESS_PORT=str(port))

    if exit_payload(host, port):
        print("exited stale payload on :%d" % port, flush=True)
    run([RELOC, host, "load"])
    run([RELOC, host, "mounts"])
    run([RELOC, host, "stop"])
    run([HOST, "--host", host, "load"], env=env)
    run([HOST, "--host", host, command] + rest, env=env)


if __name__ == "__main__":
    main()