"""Configuration, clock and working-hours helpers shared by every module."""
from __future__ import annotations

import json
import os
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path or os.environ.get("WEP_CONFIG") or ROOT / "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Ctx:
    """Everything a job needs: config, database connection and the clock."""

    cfg: dict
    conn: object = None
    _now: datetime | None = field(default=None, repr=False)  # frozen clock for tests
    _rules_cache: tuple | None = field(default=None, repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("timezone", "Asia/Kolkata"))

    @property
    def rules(self) -> dict:
        """config.yaml rules, overridden by whatever the admin saved on the Admin page (re-read every minute)."""
        if self._rules_cache and _time.monotonic() - self._rules_cache[0] < 60:
            return self._rules_cache[1]
        merged = dict(self.cfg.get("rules", {}))
        if self.conn is not None:
            row = self.conn.execute("SELECT value FROM settings WHERE key='rules'").fetchone()
            if row and row["value"]:
                merged.update(json.loads(row["value"]))
        self._rules_cache = (_time.monotonic(), merged)
        return merged

    def clear_rules_cache(self) -> None:
        self._rules_cache = None

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    def now(self) -> datetime:
        return self._now or datetime.now(self.tz).replace(microsecond=0)

    def today(self) -> date:
        return self.now().date()

    # ---- working hours -------------------------------------------------
    def _hours(self) -> tuple[time, time, set[int]]:
        wh = self.cfg.get("working_hours", {})
        start = time.fromisoformat(wh.get("start", "09:30"))
        end = time.fromisoformat(wh.get("end", "18:30"))
        days = {DAY_NAMES.index(d) for d in wh.get("days", DAY_NAMES[:5])}
        return start, end, days

    def is_working_day(self, d: date) -> bool:
        return d.weekday() in self._hours()[2]

    def day_start(self, d: date) -> datetime:
        return datetime.combine(d, self._hours()[0], self.tz)

    def day_end(self, d: date) -> datetime:
        return datetime.combine(d, self._hours()[1], self.tz)

    def next_working_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_working_day(d):
            d += timedelta(days=1)
        return d

    def roll_to_working_hours(self, dt: datetime) -> datetime:
        """Move a due time that falls outside working hours to the next working start."""
        dt = dt.astimezone(self.tz)
        d = dt.date()
        if not self.is_working_day(d) or dt >= self.day_end(d):
            return self.day_start(self.next_working_day(d))
        if dt < self.day_start(d):
            return self.day_start(d)
        return dt

    def team(self) -> list[dict]:
        return self.cfg.get("team", [])

    def member(self, member_id: str | None) -> dict | None:
        return next((m for m in self.team() if m["id"] == member_id), None)

    # Roles: GM (Growth Manager, does the calling) and SM (Service Manager, mentors GMs and gets escalations).
    @staticmethod
    def is_sm(member: dict | None) -> bool:
        return bool(member) and member.get("role") in {"sm", "manager"}

    @staticmethod
    def sm_of(member: dict | None) -> str | None:
        return (member or {}).get("sm") or (member or {}).get("manager")

    def callers(self) -> list[dict]:
        """Every active GM."""
        return [m for m in self.team() if m.get("role") in {"gm", "caller"} and m.get("active", True)]

    def managers(self) -> list[dict]:
        """Every SM."""
        return [m for m in self.team() if self.is_sm(m)]


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_dt(value, tz: ZoneInfo) -> datetime | None:
    """Parse sheet / export timestamps into tz-aware datetimes; None if blank or unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s or s.lower() in {"nan", "nat", "none"}:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            import pandas as pd

            try:
                dt = pd.to_datetime(s, dayfirst=True).to_pydatetime()
            except (ValueError, TypeError, OverflowError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).replace(microsecond=0)
