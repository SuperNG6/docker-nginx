#!/usr/bin/env python3
"""Exercise an actual nginx binary. All listeners are loopback ephemeral ports.

--mode fake: deterministic sockopt failure tests; no Brutal kernel required.
--mode real: probe forwards syscalls to the kernel; does not change routes/sysctls.
"""
import argparse
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time


def hello(name="target.example.com"):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    stream = context.wrap_bio(incoming, outgoing, server_side=False, server_hostname=name)
    try:
        stream.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def free_port(ip="127.0.0.1"):
    with socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET) as s:
        s.bind((ip, 0))
        return s.getsockname()[1]


class Backend:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.algorithms = []
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        with conn:
            conn.settimeout(5)
            self.algorithms.append(conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16))
            try:
                while data := conn.recv(65536):
                    conn.sendall(data)
            except OSError:
                pass


class Nginx:
    def __init__(self, args, backend, mode="success", workers=1, rate="1", enabled=True):
        self.args, self.backend, self.mode = args, backend, mode
        self.directory = tempfile.TemporaryDirectory(prefix="ngx-brutal-test-")
        self.root = Path(self.directory.name)
        # nginx workers may drop privileges; only this temporary directory is opened.
        self.root.chmod(0o777)
        self.port, self.port6 = free_port(), free_port("::1")
        self.trace = self.root / "trace.jsonl"
        self.trace.touch(mode=0o666)
        self.trace.chmod(0o666)
        self.log = self.root / "error.log"
        self.workers = workers
        self.write(rate, enabled)
        self.proc = None

    def write(self, rate="1", enabled=True, sni="target.example.com", extra="", preread="on", listen=None):
        directive = f"brutal_sni {sni} download={rate};" if enabled else ""
        listeners = listen or f"listen 127.0.0.1:{self.port} reuseport; listen [::1]:{self.port6} reuseport;"
        self.config = self.root / "nginx.conf"
        self.config.write_text(f'''load_module {self.args.module};
error_log {self.log} debug;
pid {self.root}/nginx.pid;
worker_processes {self.workers};
env BRUTAL_TEST_MODE;
env BRUTAL_TEST_TRACE;
events {{ worker_connections 256; }}
stream {{ server {{
{listeners}
ssl_preread {preread};
preread_timeout 2s;
proxy_timeout 5s;
{directive}
{extra}
proxy_pass 127.0.0.1:{self.backend.port};
}} }}
''')

    def command(self):
        return [self.args.nginx, "-p", str(self.root) + "/", "-c", str(self.config), "-e", "stderr"]

    def check(self):
        return subprocess.run(self.command() + ["-t"], capture_output=True, text=True)

    def __enter__(self):
        env = dict(os.environ, LD_PRELOAD=self.args.probe, BRUTAL_TEST_MODE=self.mode,
                   BRUTAL_TEST_TRACE=str(self.trace))
        self.proc = subprocess.Popen(self.command() + ["-g", "daemon off;"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise AssertionError(self.proc.stderr.read().decode())
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.1):
                    return self
            except OSError:
                time.sleep(.02)
        raise AssertionError("nginx did not listen")

    def exchange(self, name="target.example.com", fragment=False, ipv6=False, data=None, source=None):
        data = hello(name) if data is None else data
        with socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET) as c:
            c.settimeout(5)
            if source:
                c.bind((source, 0))
            c.connect(("::1" if ipv6 else "127.0.0.1", self.port6 if ipv6 else self.port))
            before = c.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16)
            if fragment:
                for a, b in [(0, 3), (3, 19), (19, 90), (90, len(data))]:
                    c.sendall(data[a:b])
                    time.sleep(.02)
            else:
                c.sendall(data)
            received = b""
            while len(received) < len(data):
                part = c.recv(len(data) - len(received))
                assert part, "proxy closed before complete echo"
                received += part
            assert received == data, "preread corrupted data"
            assert before == c.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16)

    def records(self):
        return [json.loads(x) for x in self.trace.read_text().splitlines()]

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.send_signal(signal.SIGQUIT)
            try:
                self.proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.communicate(timeout=3)
            assert self.proc.returncode == 0, f"nginx exit={self.proc.returncode}"
        if exc[0]:
            print(self.log.read_text()[-6000:] if self.log.exists() else "no log")
        self.directory.cleanup()


def config_tests(args, backend):
    n = Nginx(args, backend)
    valid = ["0.0625", "1", "12.5", "50", "125000", "0001.000001"]
    invalid = ["0", "-1", "+1", "NaN", "1e3", "50MB/s", "50M", "0.062499", "125000.000001",
               "99999999999999999999999999999999999999", "1.", ".5", "1.0000001", "1..2"]
    for rate in valid + invalid:
        n.write(rate)
        result = n.check()
        assert (result.returncode == 0) == (rate in valid), (rate, result.stderr)
    for sni in ["*.example.com", "example.com.", "-bad.com", "bad-.com", "a..com", "a" * 64 + ".com"]:
        n.write(sni=sni)
        assert n.check().returncode != 0, sni
    n.write(preread="off")
    assert n.check().returncode != 0
    n.write(extra="brutal_sni second.example.com download=1;")
    assert n.check().returncode != 0
    n.write(listen=f"listen 127.0.0.1:{n.port} udp;")
    assert n.check().returncode != 0
    n.write()
    # A second server shares group identity: conflicting rate must fail.
    text = n.config.read_text()
    second = f"server {{ listen 127.0.0.1:{free_port()}; ssl_preread on; brutal_sni TARGET.EXAMPLE.COM download=2; proxy_pass 127.0.0.1:{backend.port}; }}"
    n.config.write_text(text.rsplit("}", 1)[0] + second + "}\n")
    assert n.check().returncode != 0
    n.config.write_text(n.config.read_text().replace("download=2", "download=1"))
    assert n.check().returncode == 0
    n.directory.cleanup()
    print("PASS configuration grammar, ranges, listeners, duplicate and cross-server rules", flush=True)


def socket_tests(args, backend):
    modes = ["real"] if args.mode == "real" else ["success", "original-fail", "external", "switch-fail",
        "version-fail", "version-short", "v1", "v3", "initial-fail", "params-short", "rule",
        "set-fail", "verify-fail", "mismatch", "rollback-fail", "rollback-mismatch"]
    for mode in modes:
        with Nginx(args, backend, mode, workers=2) as n:
            for name, data in [("other.example.com", None), (None, None), (None, b"plain TCP payload\r\n")]:
                n.exchange(name, data=data)
            assert not n.records(), "unmatched sockets must not be touched"
            n.exchange(fragment=True)
            n.exchange("TARGET.EXAMPLE.COM", ipv6=True)
            n.exchange(source="127.0.0.2")
            # Exercise worker distribution and fd reuse.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: n.exchange(), range(24)))
            records = n.records()
            assert records and all(r["local"] in (n.port, n.port6) for r in records), "upstream modified"
            assert len({r["pid"] for r in records}) >= 2, "did not cover two workers"
            sets = [r for r in records if r["action"] == "set-params"]
            logs = n.log.read_text()
            if mode in ("success", "real"):
                assert sets and all(r["rc"] == 0 and r["rate"] == 1000000 for r in sets), records
                assert len({r["group"] for r in sets}) == 1 and sets[0]["group"] != 0
                assert "brutal: enabled" in logs and "brutal: step=" not in logs
                assert len(sets) == 27, "must initialize once per matched connection"
            else:
                assert "brutal: step=" in logs
                if mode in ("original-fail", "external", "switch-fail"):
                    assert not sets
                elif mode.startswith("rollback-"):
                    assert "[alert]" in logs and "restore-failed" in logs
                else:
                    assert "original-restored" in logs and "restore-failed" not in logs
                if mode in ("rule", "initial-fail", "params-short", "version-fail", "version-short", "v1", "v3"):
                    assert not sets, "must not write params on unsupported socket or external group"
                assert "suppressed=" in logs
        print(f"PASS socket mode={mode}: IPv4/IPv6, SNI, fragmentation, upstream isolation, workers, upload unchanged", flush=True)


def reload_test(args, backend):
    mode = "real" if args.mode == "real" else "success"
    with Nginx(args, backend, mode, workers=2) as n:
        n.exchange()
        group = next(r["group"] for r in n.records() if r["action"] == "set-params")
        for rate, enabled in [("2", True), ("2", False)]:
            old_pids = {r["pid"] for r in n.records()}
            n.write(rate, enabled)
            assert n.check().returncode == 0
            n.proc.send_signal(signal.SIGHUP)
            time.sleep(.5)
            before = len(n.records())
            n.exchange()
            new = n.records()[before:]
            if enabled:
                params = [r for r in new if r["action"] == "set-params"]
                assert params and params[0]["group"] == group and params[0]["rate"] == 2000000
                assert params[0]["pid"] not in old_pids
            else:
                assert not new, "disabled configuration still touching sockets"
    print("PASS reload rate and removal", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    args = parser.parse_args()
    for field in ("nginx", "module", "probe"):
        setattr(args, field, str(Path(getattr(args, field)).resolve()))
    backend = Backend()
    try:
        config_tests(args, backend)
        socket_tests(args, backend)
        reload_test(args, backend)
        assert backend.algorithms and all(b"brutal" not in a for a in backend.algorithms)
    finally:
        backend.sock.close()


if __name__ == "__main__":
    main()
