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
        if args.listen:
            mqtt_forward.do_client(args, env_config, stop_event=stop_event)
        else:
            mqtt_forward.do_server(args, env_config, stop_event=stop_event)
        _set("stopped", "session ended")
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
