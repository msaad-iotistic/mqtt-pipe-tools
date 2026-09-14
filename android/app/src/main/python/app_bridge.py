"""Android bridge: drives mqtt_forward's do_client/do_server off the UI thread.

Called from Kotlin via Chaquopy. Replaces mqtt_forward's CLI main()/stdin loop
with start()/stop()/status(). Config and status cross the bridge as JSON strings
so we never depend on Java<->Python container auto-conversion.
"""
import collections
import json
import sys
import threading

import mqtt_forward  # copied alongside this file; pulls in mqtt_cat + vendored paho

_thread = None
_stop = None
_lock = threading.Lock()
_status = {"state": "idle", "detail": ""}

# do_client/do_server (and do_send/do_receive) create their MQTTNetcat internally,
# so we capture the client by wrapping create_client — the same module-global
# monkeypatch trick used for set_progress. That gives the UI a live broker state.
_tun_client = None
_orig_forward_create_client = mqtt_forward.create_client
def _forward_create_client(*a, **k):
    global _tun_client
    _tun_client = _orig_forward_create_client(*a, **k)
    return _tun_client
mqtt_forward.create_client = _forward_create_client


def _conn_of(client):
    """Coarse broker-connection state for the UI, from a captured client.

    Mirrors mqtt_wormhole.broker_lost on any MQTTNetcat: userdata['disconnected']
    is None/0 while healthy, non-zero (or 'sub_denied') once the link drops;
    'fatal_reason' marks an ACL denial.
    """
    if client is None:
        return "connecting"
    try:
        ud = client.userdata
        if ud.get("fatal_reason"):
            return "denied"
        rc = ud.get("disconnected")
        if rc is not None and rc != 0:
            return "lost"
    except Exception:
        return "connecting"
    return "connected"


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
    # Any flags the UI has no field for (from a pasted command or the Extra args
    # box) ride along verbatim, so the full mqtt_forward CLI is reachable.
    extra = cfg.get("extra_args")
    if extra:
        import shlex
        argv += shlex.split(extra) if isinstance(extra, str) else list(extra)
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
    global _thread, _stop, _tun_client
    if _thread is not None and _thread.is_alive():
        return False
    cfg = json.loads(config_json)
    _stop = threading.Event()
    _tun_client = None  # captured afresh when do_client/do_server connects
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
        s = dict(_status)
    s["conn"] = _conn_of(_tun_client)
    return json.dumps(s)


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
    import shlex
    while toks and not toks[0].startswith("-"):
        toks.pop(0)  # strip the program name/invocation
    parser = mqtt_forward.build_parser()
    try:
        args, _ = parser.parse_known_args(toks)
    except SystemExit:
        return json.dumps({"error": "could not parse command"})

    cfg = {}
    if args.listen:
        cfg["mode"], cfg["address"] = "listen", args.listen
    elif args.connect:
        cfg["mode"], cfg["address"] = "connect", args.connect
    # Fields the UI has dedicated inputs for (incl. custom broker host/port/etc).
    for dest, key in (("code", "code"), ("broker", "broker"), ("encryption_key", "key"),
                      ("host", "host"), ("port", "port"),
                      ("username", "username"), ("password", "password")):
        v = getattr(args, dest, None)
        if v:
            cfg[key] = str(v)
    if getattr(args, "tls", False):
        cfg["tls"] = True

    # Every other flag the command set is passed straight to the forwarder via
    # extra_args, reconstructed canonically from argparse so all spellings work.
    ui_dests = {"listen", "connect", "code", "broker", "encryption_key",
                "host", "port", "username", "password", "tls", "help"}
    extra = []
    for a in parser._actions:
        if not a.option_strings or a.dest in ui_dests:
            continue
        val = getattr(args, a.dest, None)
        if val is None or val == a.default:
            continue  # not set (or set to its default → a no-op)
        flag = max(a.option_strings, key=len)  # prefer the long form
        if a.nargs == 0:  # store_true / count flags carry no value
            extra.append(flag)
        else:
            extra.extend([flag, str(val)])
    if extra:
        cfg["extra_args"] = " ".join(shlex.quote(x) for x in extra)

    if not cfg:
        return json.dumps({"error": "no --listen or --connect in command"})
    return json.dumps(cfg)


# ─── Wormhole file transfer ──────────────────────────────────────────────────
import os

import mqtt_wormhole  # copied alongside; also pulls in mqtt_cat + vendored paho

_wh_thread = None
_wh_stop = None
_wh_client = None
_wh_lock = threading.Lock()
_wh_status = {"state": "idle", "detail": "", "percent": 0, "file": "", "kind": ""}

# Capture the wormhole client too, for the broker-connection indicator.
_orig_wh_create_client = mqtt_wormhole.create_client
def _wh_create_client(*a, **k):
    global _wh_client
    _wh_client = _orig_wh_create_client(*a, **k)
    return _wh_client
mqtt_wormhole.create_client = _wh_create_client


# Surface transfer progress to the UI. make_progress_bar is the single factory
# for BOTH directions — send drives it via set_progress()->pbar.set(), receive via
# pbar.update() — so wrapping it here captures upload and download alike (the old
# set_progress-only hook missed download entirely).
_orig_make_progress_bar = mqtt_wormhole.make_progress_bar
class _ProgressProxy:
    def __init__(self, bar):
        self._bar = bar
        self._total = getattr(bar, "total", 0) or 0
        self._current = 0
    def _report(self):
        if self._total:
            with _wh_lock:
                _wh_status["percent"] = max(0, min(100, int(self._current * 100 / self._total)))
    def set(self, n):          # absolute (send path; rewinds on resend)
        self._current = n
        self._report()
        if hasattr(self._bar, "set"):
            self._bar.set(n)
        else:
            self._bar.n = n
            self._bar.refresh()
    def update(self, n):       # incremental (receive path)
        self._current += n
        self._report()
        self._bar.update(n)
    def close(self):
        self._bar.close()
    def __enter__(self):
        self._bar.__enter__()
        return self
    def __exit__(self, *a):
        return self._bar.__exit__(*a)
    def __getattr__(self, name):
        return getattr(self._bar, name)  # _bar is in __dict__, so no recursion
def _make_progress_bar_hook(total, desc):
    return _ProgressProxy(_orig_make_progress_bar(total, desc))
mqtt_wormhole.make_progress_bar = _make_progress_bar_hook


class _StderrTee:
    """Forward stderr to the real stream while keeping a tail, so we can tell why
    a transfer ended (do_send/do_receive print the reason then sys.exit(1), which
    otherwise all collapses to 'exited (1)'). Only one transfer runs at a time,
    and the markers we scan are wormhole-specific, so cross-talk is a non-issue."""
    def __init__(self, real):
        self._real = real
        self._buf = collections.deque(maxlen=8000)
    def write(self, s):
        self._real.write(s)
        self._buf.append(s)
        return len(s)
    def flush(self):
        self._real.flush()
    def tail(self):
        return "".join(self._buf)


def _classify_end(text, kind):
    """Map captured stderr to a user-facing 'disconnected' detail, or None."""
    t = text.lower()
    if "lost connection to broker" in t:
        return "broker disconnected"
    if ("ended the transfer" in t or "sender ended" in t
            or "not responding" in t or "stalled" in t):
        return "receiver disconnected" if kind == "send" else "sender disconnected"
    return None


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
    extra = cfg.get("extra_args")
    if extra:
        import shlex
        argv += shlex.split(extra) if isinstance(extra, str) else list(extra)
    return argv


def _wh_kind(kind):
    with _wh_lock:
        _wh_status["kind"] = kind


def _wh_run_send(cfg, stop_event):
    tee, old = _StderrTee(sys.stderr), sys.stderr
    try:
        _wh_kind("send")
        _wh_set("running", "sending")
        argv = [cfg["file_path"], "--code", cfg["code"]] + _conn_args(cfg)
        args = mqtt_wormhole.build_parser().parse_args(argv)
        env = mqtt_wormhole.load_env_config(args)
        sys.stderr = tee
        mqtt_wormhole.do_send(args, env, stop_event=stop_event)
        sys.stderr = old
        with _wh_lock:
            _wh_status["percent"] = 100
        _wh_set("done", "sent")
    except SystemExit as e:
        sys.stderr = old
        _wh_set("error", _classify_end(tee.tail(), "send") or "exited (%s)" % (e.code,))
    except BaseException as e:  # noqa: BLE001
        sys.stderr = old
        _wh_set("error", _classify_end(tee.tail(), "send") or "%s: %s" % (type(e).__name__, e))
    finally:
        sys.stderr = old


def _wh_run_receive(cfg, stop_event):
    tee, old = _StderrTee(sys.stderr), sys.stderr
    try:
        _wh_kind("receive")
        _wh_set("running", "receiving")
        out = cfg["out_dir"]
        os.makedirs(out, exist_ok=True)
        argv = ["--receive", "--output", out, "--code", cfg["code"],
                "--force-overwrite"] + _conn_args(cfg)
        args = mqtt_wormhole.build_parser().parse_args(argv)
        env = mqtt_wormhole.load_env_config(args)
        sys.stderr = tee
        mqtt_wormhole.do_receive(args, env, stop_event=stop_event)
        sys.stderr = old
        files = [os.path.join(out, f) for f in os.listdir(out)]
        files = [f for f in files if os.path.isfile(f)
                 and not f.endswith(".part") and not os.path.basename(f).startswith(".")]
        newest = max(files, key=os.path.getmtime) if files else ""
        with _wh_lock:
            _wh_status["percent"] = 100
        _wh_set("done", "received", file=newest)
    except SystemExit as e:
        sys.stderr = old
        _wh_set("error", _classify_end(tee.tail(), "receive") or "exited (%s)" % (e.code,))
    except BaseException as e:  # noqa: BLE001
        sys.stderr = old
        _wh_set("error", _classify_end(tee.tail(), "receive") or "%s: %s" % (type(e).__name__, e))
    finally:
        sys.stderr = old


def wormhole_new_code():
    """A fresh pairing code (e.g. 42-cosmic-dolphin) for the send UI to show."""
    return mqtt_wormhole.generate_code()


def _wh_start(target, config_json):
    global _wh_thread, _wh_stop, _wh_client
    if _wh_thread is not None and _wh_thread.is_alive():
        return False
    cfg = json.loads(config_json)
    _wh_stop = threading.Event()
    _wh_client = None  # captured afresh when do_send/do_receive connects
    with _wh_lock:
        _wh_status.update({"state": "starting", "detail": "", "percent": 0, "file": "", "kind": ""})
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
        s = dict(_wh_status)
    s["conn"] = _conn_of(_wh_client)
    return json.dumps(s)


def parse_wormhole_command(text):
    """Parse a pasted `mqtt-wormhole …` command into a Files-tab config JSON.

    Uses the real wormhole argparse. Leading program + positional (file path)
    tokens are dropped — the phone supplies its own file to send / receives by
    code — so this fills broker/key/custom + code (routed to the receive field).
    """
    import shlex
    try:
        toks = shlex.split((text or "").strip())
    except ValueError:
        toks = (text or "").split()
    while toks and not toks[0].startswith("-"):
        toks.pop(0)
    parser = mqtt_wormhole.build_parser()
    try:
        args, _ = parser.parse_known_args(toks)
    except SystemExit:
        return json.dumps({"error": "could not parse command"})

    cfg = {}
    for dest, key in (("code", "code"), ("broker", "broker"), ("encryption_key", "key"),
                      ("host", "host"), ("port", "port"),
                      ("username", "username"), ("password", "password")):
        v = getattr(args, dest, None)
        if v:
            cfg[key] = str(v)
    if getattr(args, "tls", False):
        cfg["tls"] = True

    # Everything else → extra_args, except the send/receive mechanics the app sets
    # itself (receive/output/force-overwrite) and positionals (the file path).
    ui_dests = {"code", "broker", "encryption_key", "host", "port", "username",
                "password", "tls", "help", "receive", "output", "force_overwrite"}
    extra = []
    for a in parser._actions:
        if not a.option_strings or a.dest in ui_dests:
            continue
        val = getattr(args, a.dest, None)
        if val is None or val == a.default:
            continue
        flag = max(a.option_strings, key=len)
        if a.nargs == 0:
            extra.append(flag)
        else:
            extra.extend([flag, str(val)])
    if extra:
        cfg["extra_args"] = " ".join(shlex.quote(x) for x in extra)

    if not cfg:
        return json.dumps({"error": "no recognizable wormhole options in command"})
    return json.dumps(cfg)
