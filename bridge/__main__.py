"""`python -m bridge <bridge.toml>`: load config, recover interrupted tasks, serve until SIGTERM.

Exit codes: 0 clean stop, 2 bad configuration (do not restart into it), 3 readiness preflight failed.
"""
import logging
import os
import signal
import sys
import threading

from . import config, mcp_http
from .engine import Engine
from .shell import OpenShell
from .store import Store


def main(argv, env=None, log=None):
    env = env if env is not None else os.environ
    log = log if log is not None else logging.getLogger("bridge")
    if len(argv) != 2:
        sys.stderr.write("usage: python -m bridge /etc/omp-bridge/bridge.toml\n")
        return 64
    try:
        cfg = config.load(argv[1])
        tokens = config.load_tokens(cfg.tokens_file, cfg.forgejo_identities)
    except config.ConfigError as error:
        log.error("configuration: %s", error)
        return 2

    os.makedirs(cfg.state_dir, exist_ok=True)
    os.makedirs(os.path.join(cfg.state_dir, "tmp"), exist_ok=True)
    store = Store(os.path.join(cfg.state_dir, "bridge.db"))
    shell = OpenShell(cfg.openshell_binary, cfg.openshell_gateway, env=env, sandbox=cfg.sandbox)
    engine = Engine(cfg, store, shell)
    engine.recover()
    server = mcp_http.serve(cfg, engine, tokens)

    stopping = False

    def handle_signal(signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("signal %s: stopping", signum)
        engine.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        server.serve_forever()
    finally:
        engine.stop()
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
