"""Configuration: env vars + filter defaults.

Filter thresholds and scan time are also overridable from the Google Sheet
`Config` tab — the sheet wins when both are set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Defaults:
    ivr_min: float = 50.0
    dte_min: int = 30
    dte_max: int = 60
    delta_min: float = 0.15
    delta_max: float = 0.25
    scan_time_et: str = "15:30"
    scan_window_minutes: int = 30


@dataclass(frozen=True)
class Env:
    tt_client_secret: str
    tt_refresh_token: str
    tt_account_id: str
    anthropic_api_key: str
    google_sa_json: str
    spreadsheet_id: str

    @staticmethod
    def load() -> "Env":
        def _req(key: str) -> str:
            v = os.environ.get(key, "").strip()
            if not v:
                raise RuntimeError(f"Missing required env var: {key}")
            return v

        return Env(
            tt_client_secret=_req("TASTYTRADE_CLIENT_SECRET"),
            tt_refresh_token=_req("TASTYTRADE_REFRESH_TOKEN"),
            tt_account_id=_req("TASTYTRADE_ACCOUNT_ID"),
            anthropic_api_key=_req("ANTHROPIC_API_KEY"),
            google_sa_json=_req("GOOGLE_SERVICE_ACCOUNT_JSON"),
            spreadsheet_id=_req("SPREADSHEET_ID"),
        )


DEFAULTS = Defaults()
