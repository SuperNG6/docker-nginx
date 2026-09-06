#!/usr/bin/env python3
"""Keep a real download socket across reload/rate change/disable."""
import argparse
import json
from pathlib import Path
import signal
import socket
import time

from integration import Nginx, hello
from throughput import Source


def measure(conn, duration):
    total = 0
    start = time.monotonic()
    while time.monotonic() - start < duration:
        data = conn.recv(65536)
        assert data, "old connection closed during reload"
        total += len(data)
    return total / (time.monotonic() - start) / 1000000


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nginx", required=True)
    p.add_argument("--module", required=True)
    p.add_argument("--probe", required=True)
    args = p.parse_args()
    for field in ("nginx", "module", "probe"):
        setattr(args, field, str(Path(getattr(args, field)).resolve()))
    source = Source()
    samples = []
    try:
        with Nginx(args, source, "real", workers=2) as n:
            with socket.create_connection(("127.0.0.1", n.port), timeout=5) as old:
                old.sendall(hello())
                measure(old, 2)
                samples.append(measure(old, 5))
                initial = [r for r in n.records() if r["action"] == "set-params"]
                assert len(initial) == 1
                n.write("2")
                assert n.check().returncode == 0
                n.proc.send_signal(signal.SIGHUP)
                time.sleep(.5)
                with socket.create_connection(("127.0.0.1", n.port), timeout=5) as new:
                    new.sendall(hello())
                    assert new.recv(1)
                params = [r for r in n.records() if r["action"] == "set-params"]
                assert len(params) == 2
                assert params[0]["group"] == params[1]["group"]
                assert params[0]["pid"] != params[1]["pid"]
                measure(old, 2)
                samples.append(measure(old, 5))
                n.write("2", enabled=False)
                n.proc.send_signal(signal.SIGHUP)
                time.sleep(.5)
                before = len(n.records())
                with socket.create_connection(("127.0.0.1", n.port), timeout=5) as new:
                    new.sendall(hello())
                    assert new.recv(1)
                assert len(n.records()) == before
                measure(old, 2)
                samples.append(measure(old, 5))
                assert len([r for r in n.records() if r["action"] == "set-params"]) == 2
        assert all(abs(actual / target - 1) < .1 for actual, target in zip(samples, [1, 2, 2])), samples
        print(json.dumps({"passed": True, "old_socket_MBps": samples,
                          "expected_MBps": [1, 2, 2], "connection_survived_reload_and_disable": True}))
    finally:
        source.sock.close()


if __name__ == "__main__":
    main()
