import pandas as pd
import re
from datetime import datetime as _dt
from pathlib import Path


VALID_STATUSES = {"Completed", "Assigned"}
SET_RUN_PATTERN = "Set Run"

REQUIRED_COLUMNS = [
    "Service Level",
    "Booking Time",
    "Date",
    "Sender Name",
    "Sender Suburb",
    "Sender Postcode",
    "Receiver Name",
    "Receiver Suburb",
    "Receiver Postcode",
    "Weight",
    "Qty",
    "Cubic",
    "Status",
    "Senders Ref",
]


def load_excel(file_path) -> pd.DataFrame:
    """Load raw Excel file and return a DataFrame with normalised column names."""
    df = pd.read_excel(file_path, engine="openpyxl")
    df.columns = df.columns.str.strip()
    return df


def filter_set_run(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only 'Direct - Set Run' and 'Direct - Set Run + NOT' rows."""
    mask = df["Service Level"].str.contains(SET_RUN_PATTERN, na=False, case=False)
    return df[mask].copy()


def filter_by_date(df: pd.DataFrame, target_date) -> pd.DataFrame:
    """Filter to a single delivery date. target_date may be date, str, or Timestamp."""
    date_col = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce").dt.date
    target = pd.to_datetime(target_date, dayfirst=True).date()
    return df[date_col == target].copy()


def filter_valid_status(df: pd.DataFrame) -> pd.DataFrame:
    status_mask = df["Status"].isin(VALID_STATUSES)
    return df[status_mask].copy()


def parse_booking_time(df: pd.DataFrame) -> pd.DataFrame:
    import warnings
    df = df.copy()
    # Strip trailing timezone abbreviations (e.g. "AEST", "AEDT") that pandas cannot parse
    cleaned = df["Booking Time"].astype(str).str.replace(
        r"\s+[A-Z]{2,5}$", "", regex=True
    )
    df["booking_dt"] = pd.to_datetime(cleaned, dayfirst=True, errors="coerce")
    null_mask = df["booking_dt"].isna()
    if null_mask.any():
        warnings.warn(
            f"Dropping {null_mask.sum()} row(s) with unparseable Booking Time: "
            f"{df.loc[null_mask, 'Booking Time'].tolist()}"
        )
        df = df[~null_mask].copy()
    return df


def _parse_wave_to_hhmm(wave_str) -> str | None:
    """Normalise '11AM', '5AM', '2PM' → '11:00', '05:00', '14:00' (24-h HH:MM)."""
    if pd.isna(wave_str) or not wave_str:
        return None
    try:
        return _dt.strptime(str(wave_str).upper().strip(), "%I%p").strftime("%H:%M")
    except ValueError:
        return None


def extract_driver_wave(df: pd.DataFrame) -> pd.DataFrame:
    """Parse 'Senders Ref' field for driver name and wave time (e.g. '11AM#SEAN')."""
    df = df.copy()
    ref = df["Senders Ref"].astype(str)
    df["manual_driver"] = ref.str.extract(r"#([A-Za-z]+)", expand=False).str.upper()
    raw_wave = ref.str.extract(r"^(\d+(?:AM|PM|am|pm))", expand=False).str.upper()
    df["manual_wave"] = raw_wave.apply(_parse_wave_to_hhmm)
    return df


def flag_notification(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["notification_required"] = df["Service Level"].str.contains(
        r"\+\s*NOT", na=False, case=False, regex=True
    )
    return df


def available_dates(df: pd.DataFrame) -> list:
    """Return sorted list of unique dates present in the raw DataFrame."""
    dates = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce").dt.date.dropna().unique()
    return sorted(dates)


def prepare(file_path, target_date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Full pipeline: load → filter Set Run + date + status → parse times → extract meta.

    Returns (prepared_df, raw_df) where raw_df is the full unfiltered load.
    """
    raw = load_excel(file_path)

    df = filter_set_run(raw)
    df = filter_by_date(df, target_date)
    df = filter_valid_status(df)
    df = parse_booking_time(df)
    df = extract_driver_wave(df)
    df = flag_notification(df)

    df = df.reset_index(drop=True)
    return df, raw
