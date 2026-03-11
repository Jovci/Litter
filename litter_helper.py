"""
Cloud scheduler for Litter-Robot 3 units.

Polls continuously:
  - If no completed cycle (CCC) in the last --idle-minutes → force spin.
  - If cycle in progress / interrupted for longer than --stuck-minutes → force spin.
  - Skips robots that are offline.
  - After every forced spin, refreshes and logs the new status.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pylitterbot import Account
from pylitterbot.enums import LitterBoxStatus
from pylitterbot.robot.litterrobot3 import LitterRobot3

CYCLING_STATUSES = (
    LitterBoxStatus.CLEAN_CYCLE,             # CCP — globe is spinning
    LitterBoxStatus.CAT_SENSOR_INTERRUPTED,  # CSI — spin interrupted, retrying
    LitterBoxStatus.PAUSED,                  # P   — cycle paused mid-spin
)

NATURAL_CYCLE_STATUSES = (
    LitterBoxStatus.CAT_SENSOR_TIMING,       # CST — countdown before spin starts
    LitterBoxStatus.CAT_DETECTED,            # CD  — cat is currently in the box
)

DEFAULT_POLL_SECONDS = 60
DEFAULT_IDLE_MINUTES = 60
DEFAULT_QUIET_IDLE_MINUTES = 120
DEFAULT_STUCK_MINUTES = 5
DEFAULT_CST_STUCK_MINUTES = 5
DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_QUIET_START_HOUR = 22  # 10pm
DEFAULT_QUIET_END_HOUR = 10    # 10am

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


def newest_completed_cycle(activities: list) -> datetime | None:
    """Return the timestamp of the most recent CCC entry, or None."""
    best: datetime | None = None
    for a in activities:
        if a.action is not LitterBoxStatus.CLEAN_CYCLE_COMPLETE:
            continue
        ts = a.timestamp
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if best is None or ts > best:
            best = ts
    return best


def is_within_quiet_hours(local_dt: datetime, *, start_hour: int, end_hour: int) -> bool:
    """Return True if local time is within [start_hour, end_hour), wrapping midnight if needed."""
    h = local_dt.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


async def force_spin(robot: LitterRobot3, reason: str, attempt: int = 1) -> None:
    """Attempt to unstick the robot, escalating on repeated failures.

    attempt 1: start_cleaning()
    attempt 2+: power cycle (off → wait → on), then start_cleaning()
    """
    if attempt >= 2:
        _LOGGER.warning(
            "[%s] %s — attempt #%d, escalating to POWER CYCLE",
            robot.name, reason, attempt,
        )
        _LOGGER.info("[%s] Powering OFF...", robot.name)
        await robot.set_power_status(False)
        await asyncio.sleep(10)
        _LOGGER.info("[%s] Powering ON...", robot.name)
        await robot.set_power_status(True)
        await asyncio.sleep(15)
        await robot.refresh()
        _LOGGER.info("[%s] Status after power cycle: %s (%s)", robot.name, robot.status.text, robot.status.value)
        if robot.status == LitterBoxStatus.READY:
            _LOGGER.info("[%s] Robot is Ready after power cycle, sending start_cleaning()", robot.name)
            await robot.start_cleaning()
            await asyncio.sleep(10)
            await robot.refresh()
    else:
        _LOGGER.warning("[%s] %s — sending start_cleaning()", robot.name, reason)
        await robot.start_cleaning()
        await asyncio.sleep(10)
        await robot.refresh()

    _LOGGER.info("[%s] Status after command: %s (%s)", robot.name, robot.status.text, robot.status.value)
    if robot.status in CYCLING_STATUSES:
        _LOGGER.info("[%s] Robot is now cycling — command worked", robot.name)
    elif robot.status == LitterBoxStatus.READY:
        _LOGGER.info("[%s] Robot reports Ready (may have already finished a fast cycle)", robot.name)
    else:
        _LOGGER.warning("[%s] Robot did NOT start cycling (status=%s). It may be offline or in a fault state.", robot.name, robot.status.text)


async def run_scheduled_loop(
    account: Account,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    idle_minutes: float = DEFAULT_IDLE_MINUTES,
    quiet_idle_minutes: float = DEFAULT_QUIET_IDLE_MINUTES,
    stuck_minutes: float = DEFAULT_STUCK_MINUTES,
    tz_name: str = DEFAULT_TZ,
    quiet_start_hour: int = DEFAULT_QUIET_START_HOUR,
    quiet_end_hour: int = DEFAULT_QUIET_END_HOUR,
    robot_names: list[str] | None = None,
) -> None:
    """Long-running loop: poll robots, force spin if idle, unstick if stuck."""
    stuck_since: dict[str, datetime] = {}
    cst_since: dict[str, datetime] = {}
    unstick_attempts: dict[str, int] = {}
    stuck_seconds = stuck_minutes * 60
    cst_stuck_seconds = DEFAULT_CST_STUCK_MINUTES * 60
    tz = ZoneInfo(tz_name)

    _LOGGER.info(
        "Scheduler started — poll=%ss, idle=%s min (quiet=%s min, %02d:00-%02d:00 %s), stuck=%s min",
        poll_seconds,
        idle_minutes,
        quiet_idle_minutes,
        quiet_start_hour,
        quiet_end_hour,
        tz_name,
        stuck_minutes,
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            now_local = now.astimezone(tz)
            current_idle_minutes = (
                quiet_idle_minutes
                if is_within_quiet_hours(
                    now_local, start_hour=quiet_start_hour, end_hour=quiet_end_hour
                )
                else idle_minutes
            )
            idle_seconds = current_idle_minutes * 60

            for robot in account.get_robots(LitterRobot3):
                if robot_names and robot.name not in robot_names:
                    continue

                await robot.refresh()
                rid = robot.id

                if not robot.is_online:
                    _LOGGER.debug("[%s] Offline, skipping", robot.name)
                    stuck_since.pop(rid, None)
                    continue

                _LOGGER.debug("[%s] status=%s (%s)", robot.name, robot.status.text, robot.status.value)

                # ── Cat detected: don't interfere ─────────────────
                if robot.status == LitterBoxStatus.CAT_DETECTED:
                    _LOGGER.debug("[%s] Cat in the box, skipping", robot.name)
                    stuck_since.pop(rid, None)
                    cst_since.pop(rid, None)
                    unstick_attempts.pop(rid, None)
                    continue

                # ── CST: normally 3-7 min, but can get stuck ──────
                if robot.status == LitterBoxStatus.CAT_SENSOR_TIMING:
                    stuck_since.pop(rid, None)
                    if rid not in cst_since:
                        cst_since[rid] = now
                        _LOGGER.debug("[%s] Cat Sensor Timing started, watching", robot.name)
                    else:
                        elapsed = (now - cst_since[rid]).total_seconds()
                        if elapsed >= cst_stuck_seconds:
                            _LOGGER.warning(
                                "[%s] Stuck in CST for %.1f min — power cycling",
                                robot.name, elapsed / 60,
                            )
                            await robot.set_power_status(False)
                            await asyncio.sleep(10)
                            await robot.set_power_status(True)
                            await asyncio.sleep(15)
                            await robot.refresh()
                            _LOGGER.info("[%s] Status after power cycle: %s (%s)", robot.name, robot.status.text, robot.status.value)
                            cst_since.pop(rid, None)
                        else:
                            _LOGGER.debug("[%s] CST for %.1f min (threshold %s min)", robot.name, elapsed / 60, DEFAULT_CST_STUCK_MINUTES)
                    continue

                cst_since.pop(rid, None)

                # ── Over Torque Fault: power cycle to recover ────────
                if robot.status == LitterBoxStatus.OVER_TORQUE_FAULT:
                    _LOGGER.warning("[%s] Over Torque Fault detected — power cycling", robot.name)
                    await robot.set_power_status(False)
                    await asyncio.sleep(10)
                    await robot.set_power_status(True)
                    await asyncio.sleep(15)
                    await robot.refresh()
                    _LOGGER.info("[%s] Status after power cycle: %s (%s)", robot.name, robot.status.text, robot.status.value)
                    continue

                # ── Stuck detection: globe is spinning too long ──────
                if robot.status in CYCLING_STATUSES:
                    if rid not in stuck_since:
                        stuck_since[rid] = now
                        _LOGGER.info(
                            "[%s] Cycle in progress (status=%s), will unstick if still going in %s min",
                            robot.name, robot.status.text, stuck_minutes,
                        )
                    else:
                        elapsed = (now - stuck_since[rid]).total_seconds()
                        if elapsed >= stuck_seconds:
                            attempt = unstick_attempts.get(rid, 0) + 1
                            unstick_attempts[rid] = attempt
                            await force_spin(robot, f"Stuck for {elapsed / 60:.1f} min (status={robot.status.text})", attempt)
                            stuck_since.pop(rid, None)
                    continue
                else:
                    stuck_since.pop(rid, None)
                    unstick_attempts.pop(rid, None)

                # ── Idle detection ───────────────────────────────────
                history = await robot.get_activity_history(limit=100)
                if history:
                    _LOGGER.debug(
                        "[%s] Recent activity: %s",
                        robot.name,
                        [(str(a.timestamp), a.action.text if isinstance(a.action, LitterBoxStatus) else a.action) for a in history[:5]],
                    )

                last_ccc = newest_completed_cycle(history)
                if last_ccc is None:
                    _LOGGER.info("[%s] No CCC in last %d activities", robot.name, len(history))
                    await force_spin(robot, "No completed cycle found in activity history")
                else:
                    age = (now - last_ccc).total_seconds()
                    if age >= idle_seconds:
                        await force_spin(
                            robot,
                            f"Last completed cycle {age / 60:.0f} min ago (threshold {current_idle_minutes:.0f} min)",
                        )
                    else:
                        _LOGGER.info("[%s] OK — last CCC %.1f min ago", robot.name, age / 60)

            await asyncio.sleep(poll_seconds)

        except asyncio.CancelledError:
            _LOGGER.info("Scheduler cancelled")
            raise
        except Exception:
            _LOGGER.exception("Poll failed, retrying after next interval")
            await asyncio.sleep(poll_seconds)


def main() -> None:
    p = argparse.ArgumentParser(description="Litter-Robot cloud scheduler")
    p.add_argument("--username", default=os.environ.get("LITTER_USERNAME"))
    p.add_argument("--password", default=os.environ.get("LITTER_PASSWORD"))
    p.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS, metavar="SECS",
                    help="Polling interval in seconds (default: %(default)s)")
    p.add_argument("--idle-minutes", type=float, default=DEFAULT_IDLE_MINUTES,
                    help="Force spin if no CCC in this many minutes during non-quiet hours (default: %(default)s)")
    p.add_argument("--quiet-idle-minutes", type=float, default=DEFAULT_QUIET_IDLE_MINUTES,
                    help="Force spin if no CCC in this many minutes during quiet hours (default: %(default)s)")
    p.add_argument("--tz", default=DEFAULT_TZ,
                    help="Time zone name for quiet hours (default: %(default)s)")
    p.add_argument("--quiet-start", type=int, default=DEFAULT_QUIET_START_HOUR, metavar="HOUR",
                    help="Quiet hours start hour (0-23) in --tz (default: %(default)s)")
    p.add_argument("--quiet-end", type=int, default=DEFAULT_QUIET_END_HOUR, metavar="HOUR",
                    help="Quiet hours end hour (0-23) in --tz (default: %(default)s)")
    p.add_argument("--stuck-minutes", type=float, default=DEFAULT_STUCK_MINUTES,
                    help="Unstick if in-cycle longer than this (default: %(default)s)")
    p.add_argument("--robot", action="append", dest="robot_names", metavar="NAME",
                    help="Only manage robots with this name (repeatable). Omit for all.")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="Enable DEBUG logging (shows every poll + raw activity entries)")
    args = p.parse_args()

    if not args.username or not args.password:
        p.error("--username / --password (or LITTER_USERNAME / LITTER_PASSWORD env vars) required")

    if not (0 <= args.quiet_start <= 23 and 0 <= args.quiet_end <= 23):
        p.error("--quiet-start/--quiet-end must be in range 0-23")

    if args.verbose:
        _LOGGER.setLevel(logging.DEBUG)

    async def _main() -> None:
        account = Account()
        try:
            await account.connect(username=args.username, password=args.password, load_robots=True)

            robots = account.get_robots(LitterRobot3)
            _LOGGER.info("Found %d LR3 robot(s): %s",
                         len(robots), ", ".join(f"{r.name} (online={r.is_online})" for r in robots))

            await run_scheduled_loop(
                account,
                poll_seconds=args.poll,
                idle_minutes=args.idle_minutes,
                quiet_idle_minutes=args.quiet_idle_minutes,
                stuck_minutes=args.stuck_minutes,
                tz_name=args.tz,
                quiet_start_hour=args.quiet_start,
                quiet_end_hour=args.quiet_end,
                robot_names=args.robot_names,
            )
        finally:
            await account.disconnect()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
