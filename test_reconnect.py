"""Self-check for serve_forever's reconnect/terminal decision (no network)."""
import types
import mqtt_forward


def _args(**kw):
    base = dict(no_reconnect=False, reconnect_backoff=0.0, reconnect_max_backoff=0.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_retries_transient_then_stops_on_terminal():
    seq = iter(["mqtt_lost", "peer_dead", "busy", "stopped"])
    calls = []
    def fn(a, e):
        calls.append(1); return next(seq)
    r = mqtt_forward.serve_forever(fn, _args(), {})
    assert r == "stopped", r
    assert len(calls) == 4, calls  # retried 3 transient drops, then terminal


def test_fatal_is_not_retried():
    calls = []
    def fn(a, e):
        calls.append(1); return "auth_failed"
    r = mqtt_forward.serve_forever(fn, _args(), {})
    assert r == "auth_failed" and len(calls) == 1, (r, calls)


def test_no_reconnect_passes_through():
    r = mqtt_forward.serve_forever(lambda a, e: "mqtt_lost", _args(no_reconnect=True), {})
    assert r == "mqtt_lost", r


def test_server_bye_stops_client():
    # A graceful server BYE (peer_gone) is terminal: the client stops instead of
    # reconnecting. A crashed server (peer_dead) is still retried.
    calls = []
    def fn(a, e):
        calls.append(1); return "peer_gone"
    r = mqtt_forward.serve_forever(fn, _args(), {})
    assert r == "peer_gone" and len(calls) == 1, (r, calls)


if __name__ == "__main__":
    test_retries_transient_then_stops_on_terminal()
    test_fatal_is_not_retried()
    test_no_reconnect_passes_through()
    test_server_bye_stops_client()
    print("test_reconnect: OK")
