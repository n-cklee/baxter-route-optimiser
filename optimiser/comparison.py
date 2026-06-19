import pandas as pd
from dataclasses import dataclass


@dataclass
class WaveComparison:
    wave: str
    manual_drivers: list[str]
    manual_van_count: int
    ai_van_count: int
    manual_total_hours: float   # billing hours (van_count * 9h floor)
    ai_total_hours: float
    saved_vans: int
    saved_hours: float


@dataclass
class OverallComparison:
    manual_vans: int
    ai_vans: int
    manual_billing_hours: float
    ai_billing_hours: float
    saved_vans: int
    saved_hours: float
    waves: list[WaveComparison]


def manual_plan_summary(df: pd.DataFrame) -> dict[str, list[str]]:
    """Return {wave -> [driver, driver, ...]} from the Senders Ref parsing."""
    wave_drivers: dict[str, set] = {}
    for _, row in df.iterrows():
        wave = row.get("manual_wave")
        driver = row.get("manual_driver")
        if pd.notna(wave) and pd.notna(driver):
            wave_drivers.setdefault(wave, set()).add(driver)
    return {w: sorted(d) for w, d in wave_drivers.items()}


def compare(df: pd.DataFrame, ai_routes_by_wave: dict) -> OverallComparison:
    """
    Compare manual plan (derived from Senders Ref) against AI-optimised routes.

    ai_routes_by_wave: {wave_key -> list[VanRoute]}
    """
    manual_by_wave = manual_plan_summary(df)
    wave_keys = set(manual_by_wave.keys()) | set(ai_routes_by_wave.keys())

    wave_comparisons = []
    for wave in sorted(wave_keys):
        manual_drivers = manual_by_wave.get(wave, [])
        manual_vans = len(manual_drivers)
        ai_vans = len(ai_routes_by_wave.get(wave, []))

        manual_hours = manual_vans * 9.0  # billing floor
        ai_hours = sum(r.billing_hours for r in ai_routes_by_wave.get(wave, []))

        wc = WaveComparison(
            wave=wave,
            manual_drivers=manual_drivers,
            manual_van_count=manual_vans,
            ai_van_count=ai_vans,
            manual_total_hours=manual_hours,
            ai_total_hours=ai_hours,
            saved_vans=manual_vans - ai_vans,
            saved_hours=manual_hours - ai_hours,
        )
        wave_comparisons.append(wc)

    total_manual_vans = sum(w.manual_van_count for w in wave_comparisons)
    # Count unique global van IDs (van reuse means one van may serve multiple waves)
    total_ai_vans = len(set(
        r.van_id
        for routes in ai_routes_by_wave.values()
        for r in routes
    ))
    total_manual_hours = sum(w.manual_total_hours for w in wave_comparisons)
    # Per-van billing: sum actual seconds across all waves, then apply 9h floor once per van
    van_seconds: dict[int, int] = {}
    for routes in ai_routes_by_wave.values():
        for r in routes:
            van_seconds[r.van_id] = van_seconds.get(r.van_id, 0) + r.total_seconds
    total_ai_hours = sum(max(s / 3600, 9.0) for s in van_seconds.values())

    return OverallComparison(
        manual_vans=total_manual_vans,
        ai_vans=total_ai_vans,
        manual_billing_hours=total_manual_hours,
        ai_billing_hours=total_ai_hours,
        saved_vans=total_manual_vans - total_ai_vans,
        saved_hours=total_manual_hours - total_ai_hours,
        waves=wave_comparisons,
    )
