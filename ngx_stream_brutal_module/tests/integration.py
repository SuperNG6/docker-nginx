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
    def __init__(self, args, backend, mode="success", workers=1, rate="8", enabled=True):
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

    def write(self, rate="8", enabled=True, sni="target.example.com", extra="", preread="on", listen=None, gain=None, log_level="debug"):
        directive = f"brutal_sni {sni} download_mbps={rate};" if enabled else ""
        if enabled and gain is not None:
            directive = directive[:-1] + f" gain={gain};"
        listeners = listen or f"listen 127.0.0.1:{self.port} reuseport; listen [::1]:{self.port6} reuseport;"
        self.config = self.root / "nginx.conf"
        self.config.write_text(f'''load_module {self.args.module};
error_log {self.log} notice;
pid {self.root}/nginx.pid;
worker_processes {self.workers};
env BRUTAL_TEST_MODE;
env BRUTAL_TEST_TRACE;
events {{ worker_connections 256; }}
stream {{
log_format brutal '$ssl_preread_server_name|$brutal_status|$brutal_enabled|$brutal_rate|$brutal_gain|$brutal_group|$brutal_version|$brutal_original_cc|$brutal_error|$brutal_recovery|$brutal_group_state';
access_log {self.root}/access.log brutal;
server {{
error_log {self.log} {log_level};
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

    def wait_access(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        path = self.root / "access.log"
        while True:
            # Ignore an incomplete final line while another worker is writing.
            text = path.read_text() if path.exists() else ""
            rows = [line.split("|") for line in text.split("\n")[:-1]]
            if predicate(rows):
                return rows
            if time.monotonic() >= deadline:
                raise AssertionError(f"access log condition timed out: {rows!r}")
            time.sleep(.02)

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
    valid = ["1", "8", "200", "300", "1000000", "0001"]
    invalid = ["0.5", "1.0", "300.5", "12.5", "1000001", "0", "-1", "+1", "NaN", "1e3", "50MB/s", "50M", "0.499", "1000000.001",
               "99999999999999999999999999999999999999", "1.", ".5", "1.0001", "1..2"]
    for rate in valid + invalid:
        n.write(rate)
        result = n.check()
        assert (result.returncode == 0) == (rate in valid), (rate, result.stderr)
    for sni in ["*.example.com", "example.com.", "-bad.com", "bad-.com", "a..com", "a" * 64 + ".com"]:
        n.write(sni=sni)
        assert n.check().returncode != 0, sni
    n.write(preread="off")
    assert n.check().returncode != 0
    n.write(extra="brutal_sni second.example.com download_mbps=200 gain=15;")
    assert n.check().returncode == 0
    for extra in ["brutal_sni TARGET.EXAMPLE.COM download_mbps=8;",
                  "brutal_sni second.example.com download=50;",
                  "brutal_sni second.example.com gain=20;",
                  "brutal_sni second.example.com download_mbps=1 download_mbps=2;",
                  "brutal_sni second.example.com download_mbps=1 gain=20 gain=15;",
                  "brutal_sni second.example.com download_mbps=1 unknown=2;"]:
        n.write(extra=extra)
        assert n.check().returncode != 0, extra
    for gain in ["5", "15", "20", "80", "4", "81", "-1", "1.5", "", "NaN", "999999999999999999999999"]:
        n.write(gain=gain)
        assert (n.check().returncode == 0) == (gain in ["5", "15", "20", "80"]), gain
    n.write(listen=f"listen 127.0.0.1:{n.port} udp;")
    assert n.check().returncode != 0
    n.write()
    # A second server shares group identity: conflicting rate must fail.
    text = n.config.read_text()
    second = f"server {{ listen 127.0.0.1:{free_port()}; ssl_preread on; brutal_sni TARGET.EXAMPLE.COM download_mbps=16; proxy_pass 127.0.0.1:{backend.port}; }}"
    n.config.write_text(text.rsplit("}", 1)[0] + second + "}\n")
    assert n.check().returncode != 0
    n.config.write_text(n.config.read_text().replace("download_mbps=16", "download_mbps=8"))
    assert n.check().returncode == 0
    n.config.write_text(n.config.read_text().replace("download_mbps=8;", "download_mbps=8 gain=15;", 1))
    assert n.check().returncode != 0, "cross-server gain conflict accepted"
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
            rows = n.wait_access(lambda rows: sum(
                row[0].lower() == "target.example.com" for row in rows) >= 27)
            matched = [row for row in rows if row[0].lower() == "target.example.com"]
            assert len(matched) == 27, rows
            expected = "enabled" if mode in ("success", "real") else "external" if mode == "external" else "failed"
            assert all(row[1] == expected for row in matched), matched
            assert all(row[1] == "not-enabled" for row in rows if row[0].lower() != "target.example.com")
            if mode in ("success", "real"):
                assert all(row[2:5] == ["1", "1000000", "20"] and row[8:] == ["none", "none", "applied"] for row in matched), matched
            else:
                assert all(row[2:6] == ["0", "-", "-", "-"] for row in matched), matched
                assert all(row[9] == ("restore-failed" if mode.startswith("rollback-") else
                    "not-modified" if mode in ("original-fail", "switch-fail") else
                    "external-brutal-untouched" if mode == "external" else "original-restored") for row in matched), matched
                changed = mode in ("verify-fail", "mismatch")
                assert all(row[10] == ("may-have-changed" if changed else "untouched") for row in matched), matched
            logs = n.log.read_text()
            startup = [line for line in logs.splitlines() if "brutal: module=0.2.0 built=" in line]
            assert len(startup) == 2, startup
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


def multi_sni_test(args, backend):
    mode = "real" if args.mode == "real" else "success"
    n = Nginx(args, backend, mode, workers=2)
    n.write("300", gain="15", log_level="warn",
            extra="brutal_sni second.example.com download_mbps=200 gain=80;")
    # Force delayed per-worker log flushes beyond the former 150ms sleep.
    n.config.write_text(n.config.read_text().replace(
        "access.log brutal;", "access.log brutal buffer=4k flush=1s;"))
    with n:
        n.exchange("TARGET.EXAMPLE.COM", fragment=True)
        n.exchange("SECOND.EXAMPLE.COM", ipv6=True)
        n.exchange("other.example.com")
        params = [r for r in n.records() if r["action"] == "set-params"]
        assert [r["rate"] for r in params] == [37500000, 25000000], params
        assert len({r["group"] for r in params}) == 2, params
        rows = n.wait_access(lambda rows: sum(row[1] == "enabled" for row in rows) >= 2)
        selected = [row for row in rows if row[1] == "enabled"]
        assert len(selected) == 2, rows
        assert {row[0].lower(): row[3:5] for row in selected} == {
            "target.example.com": ["37500000", "15"],
            "second.example.com": ["25000000", "80"],
        }, rows
        assert "brutal: enabled" not in n.log.read_text(), "debug logging unexpectedly enabled"
    print("PASS multiple SNI, distinct groups, gains and access variables at warn level", flush=True)


def reload_test(args, backend):
    mode = "real" if args.mode == "real" else "success"
    with Nginx(args, backend, mode, workers=2) as n:
        n.exchange()
        group = next(r["group"] for r in n.records() if r["action"] == "set-params")
        for rate, enabled in [("16", True), ("16", False)]:
            old_pids = {r["pid"] for r in n.records()}
            n.write(rate, enabled, gain="15")
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
                n.wait_access(lambda rows: any(
                    row[1:5] == ["enabled", "1", "2000000", "15"] for row in rows))
            else:
                assert not new, "disabled configuration still touching sockets"
    print("PASS reload rate and removal", flush=True)


def draining_worker_test(args, backend):
    mode = "real" if args.mode == "real" else "success"
    for rate, enabled in [("16", True), ("8", True), ("8", False)]:
        n = Nginx(args, backend, mode, workers=2)
        n.config.write_text(n.config.read_text().replace("preread_timeout 2s;", "preread_timeout 30s;"))
        with n:
            def wait_log(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if predicate(n.log.read_text()):
                        return
                    time.sleep(.02)
                raise AssertionError("worker lifecycle log timed out")
            with socket.create_connection(("127.0.0.1", n.port), timeout=5) as pending:
                data = hello()
                pending.sendall(data[:3])
                peer = pending.getsockname()[1]
                wait_log(lambda log: f"127.0.0.1:{peer} connected to" in log)
                n.write(rate, enabled)
                assert n.check().returncode == 0
                n.proc.send_signal(signal.SIGHUP)
                wait_log(lambda log: log.count("gracefully shutting down") >= 2
                         and log.count("brutal: module=") >= 4)
                n.exchange()
                writes = [r for r in n.records() if r["action"] == "set-params"]
                assert len(writes) == int(enabled), writes
                if enabled:
                    assert writes[0]["rate"] == int(rate) * 125000, writes
                pending.sendall(data[3:])
                received = b""
                while len(received) < len(data):
                    part = pending.recv(len(data) - len(received))
                    assert part, "draining worker closed a pending client"
                    received += part
                assert received == data
            rows = n.wait_access(lambda rows: any(row[1] == "skipped-worker-exiting" for row in rows))
            skipped = [row for row in rows if row[1] == "skipped-worker-exiting"]
            assert len(skipped) == 1, rows
            assert skipped[0][2:] == ["0", "-", "-", "-", "-", "-", "none", "not-modified", "untouched"], skipped
            assert not [r for r in n.records() if r["peer"] == peer], "draining client socket touched"
        print(f"PASS draining worker: rate={rate}, enabled={enabled}, late handshake proxied without socket operations", flush=True)


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
        multi_sni_test(args, backend)
        reload_test(args, backend)
        draining_worker_test(args, backend)
        assert backend.algorithms and all(b"brutal" not in a for a in backend.algorithms)
    finally:
        backend.sock.close()


if __name__ == "__main__":
    main()
