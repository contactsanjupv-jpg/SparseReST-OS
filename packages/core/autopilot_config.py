"""Autopilot configuration: one validated dataclass stored as JSON in autopilot_state.config."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.config import get_settings


class ConfigError(ValueError):
    pass


@dataclass
class AutopilotConfig:
    # --- sending limits (hard) ---
    daily_cap: int = 30
    warmup: bool = True
    warmup_schedule: list = field(default_factory=lambda: [[0, 10], [7, 20], [14, 30]])   # [days since first send, cap]
    send_days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])                         # Monday=0
    start_hour: int = 9
    end_hour: int = 17
    timezone: str = "UTC"                                                                    # the RECIPIENTS' business timezone
    min_gap_seconds: Optional[int] = None                                                    # default: window / cap
    jitter: float = 0.25
    domain_cooldown_days: int = 30
    max_task_age_days: int = 5                                                               # approved-but-unsent drafts expire
    # --- pipeline ---
    discovery_batch: int = 25
    discovery_cooldown_hours: float = 6.0
    buffer_days: float = 1.0                                                                 # keep ~this many days of qualified prospects ready
    max_new_drafts_per_tick: int = 10
    follow_up_after_days: int = 3
    # --- safety breakers ---
    bounce_pause_rate: float = 0.05
    bounce_min_sample: int = 10
    bounce_window: int = 20
    complaint_pause_rate: float = 0.08
    complaint_min_sample: int = 15
    consecutive_failure_pause: int = 3
    # --- autonomy ---
    min_founder_approvals_for_auto: int = 10
    # --- goals & reporting ---
    report_hour: int = 8
    weekly_report_weekday: int = 0

    def validate(self) -> "AutopilotConfig":
        ceiling = get_settings().outbound_hard_ceiling
        if not 1 <= self.daily_cap <= ceiling:
            raise ConfigError(f"daily_cap must be between 1 and {ceiling} (OUTBOUND_HARD_CEILING)")
        if not (0 <= self.start_hour < self.end_hour <= 24):
            raise ConfigError("start_hour must be before end_hour (0-24)")
        if not self.send_days or any(d not in range(7) for d in self.send_days):
            raise ConfigError("send_days must be a non-empty list of 0 (Mon) .. 6 (Sun)")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"unknown timezone '{self.timezone}' (use e.g. America/Chicago)") from exc
        if not 0 <= self.jitter <= 0.5:
            raise ConfigError("jitter must be between 0 and 0.5")
        if self.domain_cooldown_days < 0 or self.max_task_age_days < 1:
            raise ConfigError("domain_cooldown_days must be >= 0 and max_task_age_days >= 1")
        if self.min_gap_seconds is not None and self.min_gap_seconds < 60:
            raise ConfigError("min_gap_seconds must be at least 60 (bursts are not allowed)")
        for rate in (self.bounce_pause_rate, self.complaint_pause_rate):
            if not 0 < rate <= 1:
                raise ConfigError("pause rates must be in (0, 1]")
        return self

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AutopilotConfig":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (data or {}).items() if k != "target_customers"}      # legacy key: the goal now lives in the campaign (campaign.py)
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown autopilot setting(s): {sorted(unknown)}")
        return cls(**{k: v for k, v in data.items() if k in known}).validate()

    def to_dict(self) -> dict:
        return asdict(self)

    # ---- derived behaviour ----
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.tz)

    def effective_cap(self, first_send_at: Optional[datetime], now: datetime) -> int:
        cap = min(self.daily_cap, get_settings().outbound_hard_ceiling)
        if self.warmup and self.warmup_schedule:
            days = 0 if first_send_at is None else max(0, (now - first_send_at).days)
            allowed = [c for d, c in sorted(self.warmup_schedule) if days >= d]
            cap = min(cap, allowed[-1] if allowed else sorted(self.warmup_schedule)[0][1])
        return max(1, cap)

    def gap_seconds(self, cap: int) -> float:
        if self.min_gap_seconds:
            return float(self.min_gap_seconds)
        return max(60.0, (self.end_hour - self.start_hour) * 3600.0 / cap)

    def in_window(self, now: datetime) -> bool:
        loc = self.local(now)
        return loc.weekday() in self.send_days and self.start_hour <= loc.hour < self.end_hour

    def next_window_open(self, now: datetime) -> datetime:
        loc = self.local(now)
        for offset in range(0, 9):
            day = loc.date() + timedelta(days=offset)
            candidate = datetime.combine(day, time(self.start_hour), tzinfo=self.tz)
            if day.weekday() in self.send_days and candidate > loc:
                return candidate.astimezone(timezone.utc)
        raise ConfigError("send window never opens")

    def day_start_utc(self, now: datetime) -> datetime:
        loc = self.local(now)
        return datetime.combine(loc.date(), time(0), tzinfo=self.tz).astimezone(timezone.utc)
