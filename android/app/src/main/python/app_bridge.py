"""Android bridge: drives mqtt_forward's do_client/do_server off the UI thread.

Called from Kotlin via Chaquopy. Replaces mqtt_forward's CLI main()/stdin loop
with start()/stop()/status(). Config and status cross the bridge as JSON strings
so we never depend on Java<->Python container auto-conversion.
"""
import json
import threading

import mqtt_forward  # copied alongside this file; pulls in mqtt_cat + vendored paho

_thread = None
_stop = None
_lock = threading.Lock()
_status = {"state": "idle", "detail": ""}


def _set(state, detail=""):
    with _lock:
        _status["state"] = state
        _status["detail"] = detail


def _build_argv(cfg):
    """Translate the UI config dict into an mqtt_forward argv list."""
    mode = cfg.get("mode")
    addr = (cfg.get("address") or "").strip()
    if not addr:
        raise ValueError("address is required")
    if mode == "listen":
        if not cfg.get("code"):
            raise ValueError("listen mode needs a pairing code")
        argv = ["--listen", addr]
    elif mode == "connect":
        argv = ["--connect", addr]
    else:
        raise ValueError("mode must be 'listen' or 'connect'")

    if cfg.get("code"):
        argv += ["--code", cfg["code"]]

    broker = (cfg.get("broker") or "").strip()
    if broker:
        argv += ["--broker", broker]
    else:
        if cfg.get("host"):
            argv += ["--host", cfg["host"]]
        if cfg.get("port"):
            argv += ["--port", str(cfg["port"])]
        if cfg.get("username"):
            argv += ["--username", cfg["username"]]
        if cfg.get("password"):
            argv += ["--password", cfg["password"]]
        if cfg.get("tls"):
            argv += ["--tls"]

    if cfg.get("key"):
        argv += ["--encryption-key", cfg["key"]]
    return argv


def _run(cfg, stop_event):
    try:
        args = mqtt_forward.build_parser().parse_args(_build_argv(cfg))
        env_config = mqtt_forward.load_env_config(args)
        _set("running", "connecting")
        fn = mqtt_forward.do_client if args.listen else mqtt_forward.do_server
        # serve_forever adds auto-reconnect with backoff; stop_event ends it.
        reason = mqtt_forward.serve_forever(fn, args, env_config, stop_event=stop_event)
        _set("stopped", "ended: %s" % reason)
    except SystemExit as e:
        # do_client/do_server call sys.exit on fatal broker/ACL errors.
        _set("error", "exited (%s)" % (e.code,))
    except BaseException as e:  # noqa: BLE001 - surface everything to the UI
        _set("error", "%s: %s" % (type(e).__name__, e))


def start(config_json):
    """Start a tunnel. config_json: JSON string from Kotlin. Returns bool."""
    global _thread, _stop
    if _thread is not None and _thread.is_alive():
        return False
    cfg = json.loads(config_json)
    _stop = threading.Event()
    _set("starting")
    _thread = threading.Thread(target=_run, args=(cfg, _stop), daemon=True)
    _thread.start()
    return True


def stop():
    """Signal the running tunnel to stop (breaks the session/relay loops)."""
    if _stop is not None:
        _stop.set()
    _set("stopping")


def status():
    """Return current status as a JSON string for the UI."""
    with _lock:
        return json.dumps(_status)


def parse_command(text):
    """Parse a pasted `mqtt-forward …` command into a tunnel config JSON.

    Reuses the real argparse so every flag spelling works. Leading program tokens
    (mqtt-forward / python mqtt_forward.py / ./…) are dropped since the parser has
    no positional args. Returns {"error": …} on a bad command.
    """
    import shlex
    try:
        toks = shlex.split((text or "").strip())
    except ValueError:
        toks = (text or "").split()
    while toks and not toks[0].startswith("-"):
        toks.pop(0)  # strip the program name/invocation
    try:
        args, _ = mqtt_forward.build_parser().parse_known_args(toks)
    except SystemExit:
        return json.dumps({"error": "could not parse command"})
    cfg = {}
    if args.listen:
        cfg["mode"], cfg["address"] = "listen", args.listen
    elif args.connect:
        cfg["mode"], cfg["address"] = "connect", args.connect
    if args.code:
        cfg["code"] = args.code
    if getattr(args, "broker", None):
        cfg["broker"] = args.broker
    if getattr(args, "encryption_key", None):
        cfg["key"] = args.encryption_key
    if not cfg:
        return json.dumps({"error": "no --listen or --connect in command"})
    return json.dumps(cfg)


# ─── Wormhole file transfer ──────────────────────────────────────────────────
import os

import mqtt_wormhole  # copied alongside; also pulls in mqtt_cat + vendored paho

_wh_thread = None
_wh_stop = None
_wh_lock = threading.Lock()
_wh_status = {"state": "idle", "detail": "", "percent": 0, "file": ""}

# Surface transfer progress to the UI by wrapping set_progress (do_send/do_receive
# call it through the module global, so reassigning it here takes effect).
_orig_set_progress = mqtt_wormhole.set_progress
def _set_progress_hook(pbar, n):
    _orig_set_progress(pbar, n)
    try:
        total = getattr(pbar, "total", 0) or 0
        if total:
            with _wh_lock:
                _wh_status["percent"] = max(0, min(100, int(n * 100 / total)))
    except Exception:
        pass
mqtt_wormhole.set_progress = _set_progress_hook


def _wh_set(state, detail="", file=None):
    with _wh_lock:
        _wh_status["state"] = state
        _wh_status["detail"] = detail
        if file is not None:
            _wh_status["file"] = file


def _conn_args(cfg):
    """Broker + encryption argv shared by send and receive."""
    argv = []
    broker = (cfg.get("broker") or "").strip()
    if broker:
        argv += ["--broker", broker]
    else:
        if cfg.get("host"):
            argv += ["--host", cfg["host"]]
        if cfg.get("port"):
            argv += ["--port", str(cfg["port"])]
        if cfg.get("username"):
            argv += ["--username", cfg["username"]]
        if cfg.get("password"):
            argv += ["--password", cfg["password"]]
        if cfg.get("tls"):
            argv += ["--tls"]
    if cfg.get("key"):
        argv += ["--encryption-key", cfg["key"]]
    return argv


def _wh_run_send(cfg, stop_event):
    try:
        _wh_set("running", "sending")
        argv = [cfg["file_path"], "--code", cfg["code"]] + _conn_args(cfg)
        args = mqtt_wormhole.build_parser().parse_args(argv)
        env = mqtt_wormhole.load_env_config(args)
        mqtt_wormhole.do_send(args, env, stop_event=stop_event)
        with _wh_lock:
            _wh_status["percent"] = 100
        _wh_set("done", "sent")
    except SystemExit as e:
        _wh_set("error", "exited (%s)" % (e.code,))
    except BaseException as e:  # noqa: BLE001
        _wh_set("error", "%s: %s" % (type(e).__name__, e))


def _wh_run_receive(cfg, stop_event):
    try:
        _wh_set("running", "receiving")
        out = cfg["out_dir"]
        os.makedirs(out, exist_ok=True)
        argv = ["--receive", "--output", out, "--code", cfg["code"],
                "--force-overwrite"] + _conn_args(cfg)
        args = mqtt_wormhole.build_parser().parse_args(argv)
        env = mqtt_wormhole.load_env_config(args)
        mqtt_wormhole.do_receive(args, env, stop_event=stop_event)
        files = [os.path.join(out, f) for f in os.listdir(out)]
        files = [f for f in files if os.path.isfile(f)
                 and not f.endswith(".part") and not os.path.basename(f).startswith(".")]
        newest = max(files, key=os.path.getmtime) if files else ""
        with _wh_lock:
            _wh_status["percent"] = 100
        _wh_set("done", "received", file=newest)
    except SystemExit as e:
        _wh_set("error", "exited (%s)" % (e.code,))
    except BaseException as e:  # noqa: BLE001
        _wh_set("error", "%s: %s" % (type(e).__name__, e))


def wormhole_new_code():
    """A fresh pairing code (e.g. 42-cosmic-dolphin) for the send UI to show."""
    return mqtt_wormhole.generate_code()


def _wh_start(target, config_json):
    global _wh_thread, _wh_stop
    if _wh_thread is not None and _wh_thread.is_alive():
        return False
    cfg = json.loads(config_json)
    _wh_stop = threading.Event()
    with _wh_lock:
        _wh_status.update({"state": "starting", "detail": "", "percent": 0, "file": ""})
    _wh_thread = threading.Thread(target=target, args=(cfg, _wh_stop), daemon=True)
    _wh_thread.start()
    return True


def wormhole_send(config_json):
    """cfg: {file_path, code, broker/host..., key}. Returns True if started."""
    return _wh_start(_wh_run_send, config_json)


def wormhole_receive(config_json):
    """cfg: {out_dir, code, broker/host..., key}. Returns True if started."""
    return _wh_start(_wh_run_receive, config_json)


def wormhole_stop():
    if _wh_stop is not None:
        _wh_stop.set()
    _wh_set("stopping")


def wh_status():
    with _wh_lock:
        return json.dumps(_wh_status)
