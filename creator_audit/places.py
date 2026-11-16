"""Google Places API (New) client: Text Search for scanning, Place Details for re-fetching.

Requesting websiteUri / rating / hours puts calls on the Enterprise SKUs (spec: ~1,000 free
Text Search calls a month, then about $35 per 1,000). Only place IDs are stored; details are
re-fetched and kept in an in-memory cache for DETAILS_CACHE_SECONDS. GOOGLE_PLACES_API_KEY=fake
uses deterministic synthetic businesses so everything runs offline.
"""
from __future__ import annotations

import hashlib
import random
import time

import requests

from . import config

BASE = "https://places.googleapis.com/v1"
FIELDS = ["id", "displayName", "formattedAddress", "postalAddress", "nationalPhoneNumber", "websiteUri",
          "rating", "userRatingCount", "regularOpeningHours", "photos", "googleMapsUri", "primaryType",
          "primaryTypeDisplayName", "businessStatus"]


class PlacesError(RuntimeError):
    pass


class PlacesCapReached(PlacesError):
    pass


def _count_call(sku: str) -> None:
    """Count one call against this month's free allowance; refuse once the cap is reached."""
    from . import db
    cap = config.PLACES_MONTHLY_CAP_SEARCH if sku == "search" else config.PLACES_MONTHLY_CAP_DETAILS
    month = db.now().strftime("%Y-%m")
    row = db.q1("SELECT n FROM api_calls WHERE month=? AND sku=?", (month, "places_" + sku))
    if row and row["n"] >= cap:
        raise PlacesCapReached(f"Google Places {sku} cap reached for {month} ({cap} calls)")
    db.ex("INSERT INTO api_calls (month, sku, n) VALUES (?,?,1) ON CONFLICT(month, sku) DO UPDATE SET n=api_calls.n+1",
          (month, "places_" + sku))


def _norm(p: dict) -> dict:
    hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    pa = p.get("postalAddress") or {}
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text", ""),
        "address": p.get("formattedAddress", ""),
        "postal": {"lines": pa.get("addressLines", []), "city": pa.get("locality", ""),
                   "state": pa.get("administrativeArea", ""), "zip": pa.get("postalCode", ""),
                   "country": pa.get("regionCode", "US")},
        "phone": p.get("nationalPhoneNumber"),
        "website": p.get("websiteUri"),
        "rating": p.get("rating"),
        "reviews": p.get("userRatingCount") or 0,
        "hours": hours,
        "photos": len(p.get("photos") or []),
        "maps_uri": p.get("googleMapsUri"),
        "type": p.get("primaryType") or "",
        "type_label": (p.get("primaryTypeDisplayName") or {}).get("text", ""),
        "status": p.get("businessStatus", "OPERATIONAL"),
    }


class Places:
    def __init__(self, key: str):
        self.key = key
        self.calls = 0
        self._cache: dict[str, tuple[float, dict]] = {}

    def _headers(self, mask: str) -> dict:
        return {"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": mask, "Content-Type": "application/json"}

    def text_search(self, query: str, page_size: int = 20) -> list[dict]:
        _count_call("search")
        self.calls += 1
        r = requests.post(f"{BASE}/places:searchText", json={"textQuery": query, "pageSize": page_size},
                          headers=self._headers(",".join("places." + f for f in FIELDS)), timeout=20)
        if r.status_code != 200:
            raise PlacesError(f"searchText {r.status_code}: {r.text[:300]}")
        return [_norm(p) for p in r.json().get("places", [])]

    def details(self, place_id: str) -> dict | None:
        hit = self._cache.get(place_id)
        if hit and time.time() - hit[0] < config.DETAILS_CACHE_SECONDS:
            return hit[1]
        _count_call("details")
        self.calls += 1
        r = requests.get(f"{BASE}/places/{place_id}", headers=self._headers(",".join(FIELDS)), timeout=20)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise PlacesError(f"details {r.status_code}: {r.text[:300]}")
        d = _norm(r.json())
        self._cache[place_id] = (time.time(), d)
        return d


class FakePlaces(Places):
    NAMES = {"nail salon": ["Polished", "Glam Nails", "Luxe Nail Bar", "Pink Tips", "Nail Studio"],
             "barber shop": ["Fresh Cuts", "Kingz Barbers", "Sharp Fade", "Classic Chair", "Blade & Co"],
             "auto repair": ["Quick Fix Auto", "Precision Garage", "Bay Auto Care", "Main St Motors", "Wrench Works"],
             "restaurant": ["Casa Sol", "The Corner Table", "Mama's Kitchen", "Harbor Grill", "Bistro 7"]}
    TYPES = {"nail salon": ("nail_salon", "Nail salon"), "barber shop": ("barber_shop", "Barber shop"),
             "auto repair": ("car_repair", "Auto repair shop"), "restaurant": ("restaurant", "Restaurant")}

    def __init__(self):
        super().__init__("fake")

    def _make(self, pid: str, category: str, city: str) -> dict:
        r = random.Random(pid)
        base = self.NAMES.get(category, ["Local Business"])
        t, label = self.TYPES.get(category, ("store", "Store"))
        name = f"{r.choice(base)} {r.choice(['', 'Tampa', 'Westshore', 'Ybor', 'South'])}".strip()
        hours = None if r.random() < 0.35 else [f"{d}: 9:00 AM – 6:00 PM" for d in
                                                 ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]] + ["Sunday: Closed"]
        cty, st = (city.rsplit(" ", 1) + ["FL"])[:2]
        n = r.randint(100, 9999)
        return {"place_id": pid, "name": name, "address": f"{n} Main St, {cty}, {st} 33602, USA",
                "postal": {"lines": [f"{n} Main St"], "city": cty, "state": st, "zip": "33602", "country": "US"},
                "phone": f"(813) 555-{r.randint(1000, 9999)}", "website": None if r.random() < 0.6 else "https://example.com",
                "rating": round(r.uniform(3.2, 4.9), 1), "reviews": r.randint(0, 140), "hours": hours,
                "photos": r.randint(0, 10), "maps_uri": f"https://maps.google.com/?cid={n}", "type": t,
                "type_label": label, "status": "OPERATIONAL"}

    def text_search(self, query: str, page_size: int = 20) -> list[dict]:
        self.calls += 1
        cat = next((c for c in self.NAMES if query.lower().startswith(c)), query.split(" ")[0])
        city = query[len(cat):].strip() or "Tampa FL"
        out = []
        for i in range(min(12, page_size)):
            pid = "ChIJ" + hashlib.sha256(f"{query}{i}".encode()).hexdigest()[:23]
            d = self._make(pid, cat, city)
            FAKE_INDEX[pid] = (cat, city)
            out.append(d)
        return out

    def details(self, place_id: str) -> dict | None:
        self.calls += 1
        if place_id not in FAKE_INDEX:
            from . import db
            row = db.q1("SELECT category, city FROM businesses WHERE place_id=?", (place_id,))
            FAKE_INDEX[place_id] = (row["category"], row["city"]) if row else ("nail salon", "Tampa FL")
        cat, city = FAKE_INDEX[place_id]
        return self._make(place_id, cat, city)


FAKE_INDEX: dict[str, tuple[str, str]] = {}
_client: Places | None = None


def client() -> Places | None:
    global _client
    key = config.GOOGLE_PLACES_API_KEY
    if not key:
        return None
    if _client is None or _client.key != key:
        _client = FakePlaces() if key.lower() == "fake" else Places(key)
    return _client
