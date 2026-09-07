#!/usr/bin/env python3
"""Build a test-only variant forcing the SNI lookup to fail.

The production source and .so remain untouched; no fault hook is shipped.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from integration import Backend, Nginx


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--nginx', required=True)
    p.add_argument('--probe', required=True)
    p.add_argument('--source', required=True)
    p.add_argument('--nginx-source', required=True)
    args = p.parse_args()
    tree = Path(args.nginx_source)
    with tempfile.TemporaryDirectory(prefix='brutal-sni-failure-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        source = Path(args.source).read_text()
        lookup = 'v = ngx_stream_get_indexed_variable(s, b->variable_index);'
        assert source.count(lookup) == 1, 'SNI injection location changed'
        injected = root / 'module.c'
        injected.write_text(source.replace(lookup, 'v = NULL; /* TEST ONLY */'))
        module = root / 'sni_failure.so'
        includes = ['src/core', 'src/event', 'src/event/modules', 'src/os/unix', 'src/stream', 'objs']
        subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', *[f'-I{tree / inc}' for inc in includes],
                        str(injected), str(tree / 'objs/ngx_stream_brutal_module_modules.o'),
                        '-o', str(module)], check=True)
        backend = Backend()
        try:
            test_args = SimpleNamespace(nginx=args.nginx, probe=args.probe, module=str(module))
            n = Nginx(test_args, backend)
            n.write(extra='brutal_sni second.example.com download_mbps=200;', log_level='warn')
            with n:
                n.exchange('target.example.com')
                n.exchange('second.example.com', ipv6=True)
                rows = n.wait_access(lambda rows: sum(row[0] in (
                    'target.example.com', 'second.example.com') for row in rows) >= 2)
                # The empty readiness connection may finish during preread.
                rows = [row for row in rows if row[0] in (
                    'target.example.com', 'second.example.com')]
                assert len(rows) == 2, rows
                assert all(row[1:3] == ['failed', '0'] for row in rows), rows
                assert all(row[8:] == ['read-sni', 'not-modified', 'untouched'] for row in rows), rows
                assert not n.records(), 'SNI failure must not touch sockets'
                logs = n.log.read_text()
                failures = [line for line in logs.splitlines() if 'brutal: step=read-sni' in line]
                assert len(failures) == 1, failures  # Shared server-level rate limit.
                assert 'sni=- ' in failures[0], failures
        finally:
            backend.sock.close()
    print('PASS SNI lookup failure: session status, unknown SNI, rate limit and untouched sockets', flush=True)


if __name__ == '__main__':
    main()
