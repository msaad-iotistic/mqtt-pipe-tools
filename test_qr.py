"""Self-check for the QR pairing payload format (shared verbatim with the Android app)."""
import io
import mqtt_cat


def test_roundtrip():
    for c, b, k in [("42-cosmic-dolphin", "emqx", "K" * 32), ("x", "", ""), ("a-b", "mosquitto", "")]:
        payload = mqtt_cat.build_pairing_payload(c, b, k)
        assert payload.startswith("mqttpipe:"), payload
        d = mqtt_cat.parse_pairing_payload(payload)
        assert d.get("code") == c and d.get("broker", "") == b and d.get("key", "") == k, d


def test_plain_code():
    assert mqtt_cat.parse_pairing_payload("42-cosmic-dolphin") == {"code": "42-cosmic-dolphin"}


def test_special_chars_survive():
    p = mqtt_cat.build_pairing_payload("a b&c=d", "", "x/y+z=")
    d = mqtt_cat.parse_pairing_payload(p)
    assert d["code"] == "a b&c=d" and d["key"] == "x/y+z=", d


def test_print_qr_runs():
    buf = io.StringIO()
    mqtt_cat.print_pairing_qr("demo", "emqx", "K" * 32, file=buf)
    assert buf.getvalue().strip(), "printed nothing"


if __name__ == "__main__":
    test_roundtrip(); test_plain_code(); test_special_chars_survive(); test_print_qr_runs()
    print("test_qr: OK  (HAVE_QRCODE=%s)" % mqtt_cat.HAVE_QRCODE)
