import json
from datetime import datetime, timezone

import pytest

from wapeople import config, contact
from wapeople.districts import GEOJSON, META, label_names, party_mix


# ---- Contact form ------------------------------------------------------------------

def test_validate_requires_topic_and_detailed_message():
    clean, errors = contact.validate({"topic": "nope", "message": "too short"})
    assert set(errors) == {"topic", "message"}
    clean, errors = contact.validate({"topic": "correction", "message": "HB 1217 shows the wrong vote date for the Senate."})
    assert errors == {} and clean["topic"] == "correction"


def test_validate_strips_newlines_from_single_line_fields():
    clean, errors = contact.validate({
        "topic": "feedback", "message": "A sufficiently long message here.",
        "page": "/bills/2025-26/HB-1217\r\nBcc: victim@example.com", "name": "Pat\nSmith",
    })
    assert "\n" not in clean["page"] and "\r" not in clean["page"]
    assert clean["name"] == "Pat Smith"


def test_validate_rejects_bad_reply_address():
    for bad in ("not-an-email", "a@b", "x@example.com\nBcc: y@example.com", "a b@example.com"):
        _, errors = contact.validate({"topic": "other", "message": "x" * 30, "email": bad})
        assert "email" in errors, bad
    _, errors = contact.validate({"topic": "other", "message": "x" * 30, "email": "pat.smith+wa@example.org"})
    assert errors == {}


def test_form_token_timing_and_tampering():
    t = contact.form_token(now=1_000_000)
    assert not contact.token_ok(t, now=1_000_001)                        # sent too fast (bot)
    assert contact.token_ok(t, now=1_000_000 + 30)
    assert not contact.token_ok(t, now=1_000_000 + 2 * 86400)            # expired
    ts, sig = t.split(".")
    assert not contact.token_ok(f"{int(ts) - 100}.{sig}", now=1_000_000 + 30)  # tampered time
    assert not contact.token_ok("garbage", now=1_000_030)


def test_client_hash_does_not_reveal_ip():
    h = contact.client_hash("203.0.113.7")
    assert h == contact.client_hash("203.0.113.7") and "203" not in h and len(h) == 32


def test_email_goes_only_to_configured_recipient(monkeypatch):
    monkeypatch.setattr(config, "CONTACT_TO", "owner@example.net")
    monkeypatch.setattr(config, "SMTP_FROM", "sender@example.net")
    monkeypatch.setattr(config, "SITE_URL", "https://wapeople.example")
    msg = {"id": 7, "topic": "correction", "page": "/bills/2025-26/HB-1217", "name": "Pat",
           "reply_to": "pat@example.org", "message": "The vote date is wrong.",
           "created_at": datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)}
    e = contact.build_email(msg)
    assert e["To"] == "owner@example.net"
    assert "pat@example.org" in e["Reply-To"]
    assert "\n" not in e["Subject"]
    assert "https://wapeople.example/bills/2025-26/HB-1217" in e.get_content()


def test_send_pending_records_success_and_failure(monkeypatch):
    monkeypatch.setattr(config, "CONTACT_TO", "owner@example.net")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.net")
    monkeypatch.setattr(config, "SMTP_FROM", "sender@example.net")
    rows = [{"id": 1, "topic": "feedback", "page": None, "name": None, "reply_to": None,
             "message": "Nice site, thank you!", "created_at": datetime.now(timezone.utc)},
            {"id": 2, "topic": "other", "page": None, "name": None, "reply_to": None,
             "message": "Second message here.", "created_at": datetime.now(timezone.utc)}]
    updates = []

    class FakeConn:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("SELECT"):
                return type("R", (), {"fetchall": lambda self: rows})()
            status = sql.split("email_status = '")[1].split("'")[0]
            updates.append((status, params[-1]))
            return None

        def commit(self):
            pass

    sent = []

    def fake_send(email):
        if "Second" in email.get_content():
            raise OSError("connection refused")
        sent.append(email)

    monkeypatch.setattr(contact, "_send", fake_send)
    stats = contact.send_pending(FakeConn())
    assert stats == {"contact_sent": 1, "contact_failed": 1}
    assert updates == [("sent", 1), ("failed", 2)]


def test_send_pending_without_configuration_keeps_messages(monkeypatch):
    monkeypatch.setattr(config, "CONTACT_TO", None)
    assert contact.send_pending(object())["contact_sent"] == 0


# ---- District data -------------------------------------------------------------------

def _in_ring(pt, ring):
    inside = False
    for i in range(len(ring)):
        (xi, yi), (xj, yj) = ring[i], ring[i - 1]
        if (yi > pt[1]) != (yj > pt[1]) and pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def test_bundled_districts_are_complete_and_labels_fall_inside():
    data = json.loads(GEOJSON.read_text())
    meta = json.loads(META.read_text())
    assert "Remedial Map 3B" in meta["name"]
    feats = data["features"]
    assert sorted(f["properties"]["district"] for f in feats) == list(range(1, 50))
    for f in feats:
        pt = (f["properties"]["label_lon"], f["properties"]["label_lat"])
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        assert any(_in_ring(pt, p[0]) and not any(_in_ring(pt, h) for h in p[1:]) for p in polys), \
            f"label for district {f['properties']['district']} is outside it"
        assert -124.9 < pt[0] < -116.9 and 45.5 < pt[1] < 49.01


def test_party_mix_and_labels():
    d = {"senators": [{"name": "A", "party": "D"}], "representatives": [{"name": "B", "party": "D"}, {"name": "C", "party": "R"}]}
    assert party_mix(d) == "split"
    assert party_mix({"senators": [{"party": "R"}], "representatives": [{"party": "R"}]}) == "R"
    assert party_mix({"senators": [], "representatives": []}) == "vacant"
    assert label_names(d["representatives"]) == "B (D), C (R)"
