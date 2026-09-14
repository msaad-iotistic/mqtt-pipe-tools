"""Self-checks for app_bridge's pure logic (no Android, no network).

Run from the repo root:  python android/test_app_bridge.py
Adds repo root + the app's python dir to sys.path so app_bridge can import the
engine modules exactly as it does on-device.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))                 # repo root (mqtt_*.py)
sys.path.insert(0, os.path.join(_HERE, "app", "src", "main", "python"))
import app_bridge  # noqa: E402
import mqtt_wormhole  # noqa: E402


def test_parse_wormhole_command_extracts_all_flags():
    key = "k" * 32
    cmd = ("mqtt-wormhole f.txt --code 4-a-b-c --broker emqx --host h.example "
           "--port 8883 --tls --username u --password p -e " + key + " --qos 1")
    o = json.loads(app_bridge.parse_wormhole_command(cmd))
    assert o["code"] == "4-a-b-c" and o["broker"] == "emqx", o
    assert o["host"] == "h.example" and o["port"] == "8883" and o["tls"] is True, o
    assert o["username"] == "u" and o["password"] == "p" and o["key"] == key, o
    assert "--qos" in o["extra_args"] and "1" in o["extra_args"], o
    # the leading program + file positional are dropped
    assert "f.txt" not in json.dumps(o), o
    # The UI clears the preset when a custom host is set (applyFilesConfig), so the
    # built argv uses the host; re-parse it through the real wormhole parser.
    cfg = dict(o)
    cfg.pop("broker", None)  # host overrides preset
    argv = ["--receive", "--output", "/tmp/out", "--code", cfg["code"]] + app_bridge._conn_args(cfg)
    a = mqtt_wormhole.build_parser().parse_args(argv)
    assert a.host == "h.example" and a.port == "8883" and a.tls and a.qos == 1, a


def test_parse_wormhole_receive_command():
    o = json.loads(app_bridge.parse_wormhole_command(
        "mqtt-wormhole --receive --code 9-z-y-x --broker emqx --qr"))
    assert o["code"] == "9-z-y-x" and o["broker"] == "emqx", o
    assert o.get("extra_args") == "--qr", o  # receive/output/force-overwrite excluded


def test_parse_bad_command():
    o = json.loads(app_bridge.parse_wormhole_command("hello world"))
    assert "error" in o, o


def test_conn_of():
    class C:  # minimal MQTTNetcat stand-in
        def __init__(self, ud): self.userdata = ud
    assert app_bridge._conn_of(None) == "connecting"
    assert app_bridge._conn_of(C({"disconnected": None})) == "connected"
    assert app_bridge._conn_of(C({"disconnected": 0})) == "connected"
    assert app_bridge._conn_of(C({"disconnected": 1})) == "lost"
    assert app_bridge._conn_of(C({"disconnected": "sub_denied", "fatal_reason": "ACL"})) == "denied"


def test_classify_end():
    c = app_bridge._classify_end
    assert c("Error: lost connection to broker.", "send") == "broker disconnected"
    assert c("Receiver ended the transfer: bye", "send") == "receiver disconnected"
    assert c("ConnectionError: sender ended: x", "receive") == "sender disconnected"
    assert c("sender not responding (disconnected?)", "receive") == "sender disconnected"
    assert c("transfer stalled at chunk 3", "send") == "receiver disconnected"
    assert c("some unrelated error", "send") is None


def test_progress_proxy_reports_both_directions():
    # make_progress_bar is wrapped to a proxy that reports on set() AND update().
    bar = mqtt_wormhole.make_progress_bar(1000, "t")
    with bar as p:
        p.update(250)
        assert app_bridge._wh_status["percent"] == 25, app_bridge._wh_status
        p.set(500)                       # absolute (send path)
        assert app_bridge._wh_status["percent"] == 50, app_bridge._wh_status
        p.update(500)                    # increment (receive path)
        assert app_bridge._wh_status["percent"] == 100, app_bridge._wh_status


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_app_bridge: OK")
