import csv
import json
import time
from pathlib import Path
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

CACHE_PATH = Path(__file__).parent.parent / "cache" / "geocode_cache.csv"
_LEGACY_JSON_PATH = Path(__file__).parent.parent / "cache" / "geocode_cache.json"
NOMINATIM_UA = "baxter-route-optimiser/1.0"
REQUEST_DELAY = 1.1  # seconds between Nominatim calls (rate limit: 1/s)


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open(newline="") as f:
                return {
                    row["key"]: {"lat": float(row["lat"]), "lng": float(row["lng"])}
                    for row in csv.DictReader(f)
                }
        except (OSError, KeyError, ValueError):
            return {}
    # One-time migration from legacy JSON cache
    if _LEGACY_JSON_PATH.exists():
        try:
            data = json.loads(_LEGACY_JSON_PATH.read_text())
            _save_cache(data)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "lat", "lng"])
        for k, v in sorted(cache.items()):
            writer.writerow([k, v["lat"], v["lng"]])


def _cache_key(suburb: str, postcode) -> str:
    return f"{suburb.strip().upper()}|{str(postcode).strip()}"


def geocode_stops(stops: list[dict], progress_callback=None) -> dict:
    """
    Geocode a list of stop dicts with keys 'suburb' and 'postcode'.

    Returns a dict keyed by cache_key -> {"lat": float, "lng": float} or None on failure.
    Failed lookups are stored as None in the returned dict (and NOT cached) so callers
    can warn the user without blocking the optimisation.
    """
    cache = _load_cache()
    geolocator = Nominatim(user_agent=NOMINATIM_UA, timeout=10)

    # Deduplicate — only geocode each unique suburb/postcode once
    unique: dict[str, dict] = {}
    for s in stops:
        key = _cache_key(s["suburb"], s["postcode"])
        if key not in unique:
            unique[key] = s

    results = {}
    need_lookup = {k: v for k, v in unique.items() if k not in cache}
    total = len(need_lookup)

    for i, (key, stop) in enumerate(need_lookup.items()):
        if progress_callback:
            progress_callback(i, total, stop["suburb"])

        query = f"{stop['suburb'].title()} NSW {stop['postcode']} Australia"
        try:
            time.sleep(REQUEST_DELAY)
            location = geolocator.geocode(query)
            if location:
                cache[key] = {"lat": location.latitude, "lng": location.longitude}
            else:
                # Try without postcode as fallback
                time.sleep(REQUEST_DELAY)
                location = geolocator.geocode(f"{stop['suburb'].title()} NSW Australia")
                if location:
                    cache[key] = {"lat": location.latitude, "lng": location.longitude}
                else:
                    results[key] = None
                    continue
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            results[key] = None
            continue

    _save_cache(cache)

    # Merge cache into results
    for key in unique:
        if key not in results:
            results[key] = cache.get(key)

    return results


def attach_coords(df, suburb_col: str, postcode_col: str, geocoded: dict):
    """
    Add lat/lng columns to a DataFrame from the geocoded lookup dict.
    Rows that failed geocoding get NaN coords.
    """
    import pandas as pd

    df = df.copy()

    def lookup(row):
        key = _cache_key(row[suburb_col], row[postcode_col])
        result = geocoded.get(key)
        if result:
            return pd.Series([result["lat"], result["lng"]])
        return pd.Series([None, None])

    df[["lat", "lng"]] = df.apply(lookup, axis=1)
    return df


def failed_geocodes(geocoded: dict) -> list[str]:
    return [k for k, v in geocoded.items() if v is None]
