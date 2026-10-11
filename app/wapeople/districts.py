"""Legislative district boundaries and each district's current delegation.

Boundaries come from the official Washington State Office of Financial Management dataset
(see data/wa_legislative_districts_2024.meta.json and scripts/build_districts.sh) and are
stored in the legislative_districts table. Each district elects one senator and two
representatives.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data"
GEOJSON = DATA / "wa_legislative_districts_2024.geojson"
META = DATA / "wa_legislative_districts_2024.meta.json"
SEATS = {"Senate": 1, "House": 2}


def ensure_loaded(conn: psycopg.Connection) -> bool:
    """Load the bundled boundaries if the table is empty or holds a different plan.
    Returns True when it (re)loaded."""
    meta = json.loads(META.read_text())
    row = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE plan = %s) AS same FROM legislative_districts",
        (meta["name"],),
    ).fetchone()
    if row["n"] == 49 and row["same"] == 49:
        return False
    features = json.loads(GEOJSON.read_text())["features"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM legislative_districts")
        cur.executemany(
            "INSERT INTO legislative_districts (district, plan, geometry, label_lon, label_lat, source)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            [(f["properties"]["district"], meta["name"], Jsonb(f["geometry"]),
              f["properties"]["label_lon"], f["properties"]["label_lat"], Jsonb(meta))
             for f in features],
        )
    log.info("loaded %d legislative districts (%s)", len(features), meta["name"])
    return True


def current_biennium_with_roster(conn: psycopg.Connection) -> str | None:
    row = conn.execute(
        "SELECT max(biennium) AS b FROM member_terms WHERE district ~ '^[0-9]+$'"
    ).fetchone()
    return row["b"] if row else None


def delegations(conn: psycopg.Connection, biennium: str) -> dict[int, dict]:
    """district -> {'senators': [...], 'representatives': [...]}.

    The roster lists everyone who served during the biennium, including members who left
    mid-term. For each seat we keep the members who voted most recently, so replacements
    (appointments after a resignation) show instead of the people they replaced.
    """
    rows = conn.execute(
        """
        WITH lv AS (
            SELECT v.member_id, max(rc.vote_date) AS last_vote
            FROM votes v JOIN roll_calls rc ON rc.id = v.roll_call_id
            WHERE rc.biennium = %(b)s
            GROUP BY v.member_id
        )
        SELECT t.district::int AS district, t.agency, m.id, m.name, m.last_name, t.party,
               lv.last_vote,
               row_number() OVER (PARTITION BY t.district, t.agency
                                  ORDER BY lv.last_vote DESC NULLS LAST, m.id) AS rn
        FROM member_terms t
        JOIN members m ON m.id = t.member_id
        LEFT JOIN lv ON lv.member_id = t.member_id
        WHERE t.biennium = %(b)s AND t.district ~ '^[0-9]+$' AND t.agency IN ('Senate', 'House')
        """,
        {"b": biennium},
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        if r["rn"] > SEATS[r["agency"]]:
            continue
        d = out.setdefault(r["district"], {"senators": [], "representatives": []})
        key = "senators" if r["agency"] == "Senate" else "representatives"
        d[key].append({"id": r["id"], "name": r["name"], "last_name": r["last_name"], "party": r["party"]})
    for d in out.values():
        d["representatives"].sort(key=lambda m: (m["last_name"] or m["name"]))
    return out


def party_mix(delegation: dict) -> str:
    parties = {m["party"] for m in delegation["senators"] + delegation["representatives"]}
    if parties == {"D"}:
        return "D"
    if parties == {"R"}:
        return "R"
    return "split" if parties else "vacant"


def map_geojson(conn: psycopg.Connection) -> dict:
    """District polygons with their current legislators, ready for the map."""
    biennium = current_biennium_with_roster(conn)
    dels = delegations(conn, biennium) if biennium else {}
    feats = []
    meta = None
    for r in conn.execute(
        "SELECT district, geometry, label_lon, label_lat, source FROM legislative_districts ORDER BY district"
    ):
        meta = r["source"]
        dl = dels.get(r["district"], {"senators": [], "representatives": []})
        feats.append({
            "type": "Feature",
            "id": r["district"],
            "geometry": r["geometry"],
            "properties": {
                "district": r["district"],
                "label_lon": r["label_lon"],
                "label_lat": r["label_lat"],
                "mix": party_mix(dl),
                # Flat strings for map labels; structured lists for the side panel.
                "senator": label_names(dl["senators"]),
                "reps": label_names(dl["representatives"]),
                "senators": dl["senators"],
                "representatives": dl["representatives"],
            },
        })
    return {"type": "FeatureCollection", "features": feats, "biennium": biennium, "source": meta}


def label_names(members: list[dict]) -> str:
    return ", ".join(f"{m['name']} ({m['party'] or '?'})" for m in members)
