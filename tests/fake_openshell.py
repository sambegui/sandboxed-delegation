#!/usr/bin/env python3
"""A fake `openshell` CLI for the bridge tests. State lives under $FAKE_DIR; /sandbox paths map into
$FAKE_DIR/sandbox (argv elements and --workdir are rewritten), so the bridge drives a real local tree
with real git, real FIFOs and a real fake omp. Every call is logged to $FAKE_DIR/calls.jsonl.

usage: fake_openshell.py -g <gateway> sandbox exec -n <name> --no-tty --no-login-shell --workdir <dir> -- <argv...>
       fake_openshell.py -g <gateway> sandbox upload <name> <local> <remote>
"""
import json
import os
import pathlib
import subprocess
import sys

STATE = pathlib.Path(os.environ["FAKE_DIR"])
FAKE_FS = STATE / "sandbox"


def map_path(value):
    if value == "/sandbox" or value.startswith("/sandbox/"):
        return str(FAKE_FS) + value[len("/sandbox"):]
    return value


def main():
    args = sys.argv[1:]
    assert args[:2] == ["-g", os.environ.get("FAKE_GATEWAY", "realm2")], args
    args = args[2:]
    with open(STATE / "calls.jsonl", "a") as log:
        log.write(json.dumps(args) + "\n")
    assert args[0] == "sandbox", args
    verb = args[1]
    if verb == "exec":
        workdir = "/sandbox"
        rest = args[2:]
        while rest and rest[0] != "--":
            if rest[0] in ("-n", "--name"):
                rest = rest[2:]
                continue
            if rest[0] == "--workdir":
                workdir = rest[1]
                rest = rest[2:]
                continue
            if rest[0] in ("--no-tty", "--no-login-shell"):
                rest = rest[1:]
                continue
            sys.stderr.write(f"fake openshell: unexpected exec flag {rest[0]!r}\n")
            return 64
        assert rest[0] == "--", rest
        argv = [map_path(a) for a in rest[1:]]
        cwd = map_path(workdir)
        os.makedirs(cwd, exist_ok=True)
        try:
            done = subprocess.run(argv, cwd=cwd, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=120, start_new_session=True)
        except subprocess.TimeoutExpired:
            return 124
        sys.stdout.buffer.write(done.stdout)
        sys.stderr.buffer.write(done.stderr)
        return done.returncode
    if verb == "upload":
        _, local, remote = args[2], args[3], args[4]
        target = pathlib.Path(map_path(remote))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pathlib.Path(local).read_bytes())
        print("Upload complete")
        return 0
    if verb == "download":
        _, remote, local = args[2], args[3], args[4]
        source = pathlib.Path(map_path(remote))
        target = pathlib.Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            import shutil
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.write_bytes(source.read_bytes())
        print("Download complete")
        return 0
    sys.stderr.write(f"fake openshell: unexpected command {args!r}\n")
    return 64


if __name__ == "__main__":
    sys.exit(main())
