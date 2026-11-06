"""Pure helpers for reading, editing and validating an ecobee thermostat program.

This module deliberately imports nothing from Home Assistant or pyecobee so it can be
unit-tested with plain pytest. Everything here works on the plain ``dict`` shape the
ecobee API returns for ``thermostat["program"]``::

    {
        "schedule": [[climateRef x 48] x 7],
        "climates": [{"climateRef": "home", "name": "Home", "heatTemp": 700, ...}, ...],
        "currentClimateRef": "home",   # read-only, stripped before writing
    }

Facts that shape this module (see BRIEF.md and the ecobee developer docs):

* Temperatures are integers in TENTHS of a degree Fahrenheit (700 == 70.0 °F)
  regardless of the user's display unit.
* ``schedule`` is 7 lists of 48 climateRef strings; slot 0 is 00:00-00:30, slot 47 is
  23:30-24:00.
* Day order: index 0 is MONDAY, index 6 is SUNDAY. Source: ecobee API example 11,
  "Changing your thermostat's program",
  https://www.ecobee.com/home/developer/api/examples/ex11.shtml — "The schedule is an
  array of 7 elements. Each element represents a day within the week starting with
  Monday. Each day is split into 48 thirty minutes slots. Each slot is a string
  reference to a climate." The newer ecobee SmartBuildings schedule schema
  (https://docs.sb.ecobee.com/reference/getthermostatschedule) also lists the days
  monday..sunday. NOTE: the Core Concepts page
  (https://www.ecobee.com/home/developer/api/introduction/core-concepts.shtml) shows an
  illustrative table with columns Sun..Sat, and BRIEF.md initially assumed
  index 0 = Sunday; the explicit statement in example 11 wins. If a live thermostat
  ever disagrees, change ``SCHEDULE_DAYS`` below — nothing else hard-codes the order.
* The whole program (all climates + full schedule) must be sent on every write.
  climateRef is server-generated: supply it for existing climates, omit it for new ones.
  Climate names must be unique. For climate sensors only ``id`` is mandatory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import copy
import re
from typing import Any

# --------------------------------------------------------------------------- constants

UNIT_FAHRENHEIT = "°F"
UNIT_CELSIUS = "°C"

#: Day keys in ecobee schedule order. Index 0 = Monday (see module docstring).
SCHEDULE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_INDEX: dict[str, int] = {day: i for i, day in enumerate(SCHEDULE_DAYS)}

#: Group keys accepted in a schedule spec, expanded to concrete days.
DAY_GROUPS: dict[str, tuple[str, ...]] = {
    "weekdays": ("mon", "tue", "wed", "thu", "fri"),
    "weekend": ("sat", "sun"),
    "all": SCHEDULE_DAYS,
}

#: Long names are accepted too (monday, Tuesday, ...), normalised to the 3-letter key.
_DAY_ALIASES: dict[str, str] = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
    "weekdays": "weekdays",
    "weekday": "weekdays",
    "weekend": "weekend",
    "weekends": "weekend",
    "all": "all",
    "everyday": "all",
    "daily": "all",
}

SLOTS_PER_DAY = 48
SLOT_MINUTES = 30
DAYS_PER_WEEK = 7

#: Comfort-setting bounds enforced before a write (°F, tenths) when the thermostat's
#: own ``settings`` ranges are unknown; see :func:`temp_ranges_for`.
MIN_TEMP_TENTHS = 450  # 45 °F
MAX_TEMP_TENTHS = 920  # 92 °F
#: Minimum cool - heat gap when the thermostat can both heat and cool and its
#: ``settings.heatCoolMinDelta`` is unknown; see :func:`deadband_for`.
DEFAULT_DEADBAND_TENTHS = 20  # 2 °F

#: ecobee hvacMode values for which the deadband is not enforced. The Settings doc says
#: heatCoolMinDelta is "used to ensure that when thermostat is in auto mode, the heat and
#: cool values are separated by at least this value"; core climate.py groups heat, cool,
#: auxHeatOnly and off together for the same reason.
SINGLE_MODE_HVAC = frozenset({"heat", "cool", "auxHeatOnly", "off"})

#: Default climateRefs that get number entities.
DEFAULT_CLIMATE_REFS: tuple[str, ...] = ("home", "away", "sleep")

#: climateRefs ecobee assigns identically on every thermostat (residential home/away/
#: sleep, commercial occupied/unoccupied, legacy wakeup). Any other ref (``smart1``,
#: ``smart2``, ...) is server-generated per thermostat in creation order, so the same ref
#: on two thermostats is usually a different comfort setting. ``copy_program`` therefore
#: matches only these by ref and everything else by name.
STANDARD_CLIMATE_REFS = frozenset(
    {"home", "away", "sleep", "wakeup", "occupied", "unoccupied"}
)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ProgramError(ValueError):
    """Raised for any invalid program edit or spec; message is user-facing."""


# ------------------------------------------------------------------- temperatures


def temp_to_tenths(value: float, unit: str = UNIT_FAHRENHEIT) -> int:
    """Convert a user temperature in ``unit`` to ecobee tenths of °F."""
    if unit == UNIT_CELSIUS:
        fahrenheit = value * 9.0 / 5.0 + 32.0
    elif unit == UNIT_FAHRENHEIT:
        fahrenheit = float(value)
    else:
        raise ProgramError(f"Unsupported temperature unit {unit!r}")
    return int(round(fahrenheit * 10.0))


def tenths_to_temp(tenths: int, unit: str = UNIT_FAHRENHEIT) -> float:
    """Convert ecobee tenths of °F to a user temperature in ``unit``.

    °F values are rounded to one decimal (they are exact tenths anyway); °C values to
    one decimal as well, which is what the ecobee app shows.
    """
    fahrenheit = tenths / 10.0
    if unit == UNIT_FAHRENHEIT:
        return round(fahrenheit, 1)
    if unit == UNIT_CELSIUS:
        return round((fahrenheit - 32.0) * 5.0 / 9.0, 1)
    raise ProgramError(f"Unsupported temperature unit {unit!r}")


# ---------------------------------------------------------------------- climates


def find_climate(program: Mapping[str, Any], ref_or_name: str) -> dict[str, Any]:
    """Return the climate dict matching ``ref_or_name``.

    Matches climateRef exactly first, then climateRef case-insensitively, then the
    display name case-insensitively ("Home" -> climateRef "home").
    """
    climates = program.get("climates", [])
    for climate in climates:
        if climate.get("climateRef") == ref_or_name:
            return climate
    wanted = ref_or_name.strip().casefold()
    for climate in climates:
        if str(climate.get("climateRef", "")).casefold() == wanted:
            return climate
    for climate in climates:
        if str(climate.get("name", "")).casefold() == wanted:
            return climate
    raise ProgramError(
        f"Unknown climate {ref_or_name!r}; available: {', '.join(climate_refs(program))}"
    )


def climate_refs(program: Mapping[str, Any]) -> list[str]:
    """Return the climateRefs of a program in order."""
    return [c["climateRef"] for c in program.get("climates", []) if "climateRef" in c]


def resolve_sensors(
    tokens: Iterable[str], remote_sensors: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """Map sensor names or ids to climate ``sensors`` entries.

    ``remote_sensors`` is ``thermostat["remoteSensors"]`` (ids like ``ei:0`` or
    ``rs2:100``). Climate sensor entries use the temperature-capability id, which is the
    sensor id with ``:1`` appended (``ei:0:1``) — the same convention pyecobee's
    ``update_climate_sensors`` uses. Tokens may be a name (case-insensitive), the sensor
    id, or the capability id.
    """
    resolved: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        key = str(token).strip()
        match: Mapping[str, Any] | None = None
        for sensor in remote_sensors:
            sid = str(sensor.get("id", ""))
            if key in (sid, f"{sid}:1") or (
                sensor.get("name") and str(sensor["name"]).casefold() == key.casefold()
            ):
                match = sensor
                break
        if match is None:
            names = ", ".join(
                f"{s.get('name')} ({s.get('id')})" for s in remote_sensors if s.get("id")
            )
            raise ProgramError(f"Unknown sensor {token!r}; available: {names}")
        cap_id = f"{match['id']}:1"
        if cap_id in seen:
            continue
        seen.add(cap_id)
        entry = {"id": cap_id}
        if match.get("name"):
            entry["name"] = str(match["name"])
        resolved.append(entry)
    if not resolved:
        raise ProgramError("At least one sensor must participate in a climate")
    return resolved


# ------------------------------------------------------------------- schedule spec


def normalize_day_key(key: str) -> str:
    """Return the canonical day or group key for ``key`` or raise ProgramError."""
    k = str(key).strip().casefold()
    if k in DAY_INDEX or k in DAY_GROUPS:
        return k
    if k in _DAY_ALIASES:
        return _DAY_ALIASES[k]
    raise ProgramError(
        f"Unknown day key {key!r}; use one of "
        f"{', '.join(SCHEDULE_DAYS)}, {', '.join(DAY_GROUPS)}"
    )


def expand_day_keys(keys: Iterable[str]) -> dict[str, str]:
    """Expand day/group keys to concrete days.

    Returns ``{day: source_key}``. A concrete day given explicitly overrides any group
    that also covers it ("weekdays" + "fri" -> fri uses the fri spec). Two groups that
    overlap (e.g. "all" + "weekend") are resolved by specificity: all < weekdays/weekend
    < concrete day.
    """
    rank = {"all": 0, "weekdays": 1, "weekend": 1}
    result: dict[str, str] = {}
    result_rank: dict[str, int] = {}
    for raw in keys:
        key = normalize_day_key(raw)
        days = DAY_GROUPS.get(key, (key,))
        this_rank = rank.get(key, 2)
        for day in days:
            if day in result and result_rank[day] == this_rank:
                raise ProgramError(
                    f"Day {day!r} is specified twice ({result[day]!r} and {key!r})"
                )
            if day not in result or this_rank > result_rank[day]:
                result[day] = key
                result_rank[day] = this_rank
    return result


def parse_time_to_slot(text: str) -> int:
    """Parse ``HH:MM`` and return the half-hour slot index, snapping to 30 minutes.

    Snapping rounds to the nearest half hour (07:10 -> 07:00, 07:20 -> 07:30,
    23:50 -> slot 47, never past the end of the day).
    """
    m = _TIME_RE.match(str(text).strip())
    if not m:
        raise ProgramError(f"Invalid time {text!r}; expected HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ProgramError(f"Invalid time {text!r}; expected 00:00 to 23:59")
    total = hour * 60 + minute
    # Half-up integer rounding: Python's round() is half-to-even, which sent 06:45 and
    # 07:15 to the same slot (07:00) and made :15/:45 snap inconsistently.
    slot = (total + SLOT_MINUTES // 2) // SLOT_MINUTES
    return min(slot, SLOTS_PER_DAY - 1)


def slot_to_time(slot: int) -> str:
    """Return ``HH:MM`` for a half-hour slot index."""
    if not 0 <= slot < SLOTS_PER_DAY:
        raise ProgramError(f"Slot {slot} out of range 0..{SLOTS_PER_DAY - 1}")
    minutes = slot * SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_day_spec(spec: str) -> list[tuple[int, str]]:
    """Parse ``"07:00 home, 21:00 sleep"`` into ``[(slot, token), ...]``.

    Entries are comma- or semicolon-separated ``TIME CLIMATE`` pairs. Times must be
    strictly increasing after snapping. Climate tokens are returned as written; resolve
    them with :func:`find_climate`.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ProgramError("Schedule spec must be a non-empty string")
    entries: list[tuple[int, str]] = []
    for part in re.split(r"[,;]", spec):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(None, 1)
        if len(pieces) != 2:
            raise ProgramError(f"Invalid schedule entry {part!r}; expected 'HH:MM climate'")
        slot = parse_time_to_slot(pieces[0])
        token = pieces[1].strip()
        if entries and slot <= entries[-1][0]:
            raise ProgramError(
                f"Schedule times must increase; {pieces[0]!r} does not follow "
                f"{slot_to_time(entries[-1][0])}"
            )
        entries.append((slot, token))
    if not entries:
        raise ProgramError("Schedule spec must contain at least one 'HH:MM climate' entry")
    return entries


def expand_day(entries: Sequence[tuple[int, str]], fill_ref: str | None) -> list[str]:
    """Expand parsed entries to 48 climateRefs.

    Slots before the first entry get ``fill_ref`` (the climate carried over from the
    previous day's last slot). If the first entry is at 00:00, ``fill_ref`` is unused.
    """
    if not entries:
        raise ProgramError("Cannot expand an empty day")
    first_slot = entries[0][0]
    if first_slot > 0 and fill_ref is None:
        raise ProgramError("Day spec must start at 00:00 when no previous day exists")
    day: list[str] = []
    current = fill_ref if first_slot > 0 else entries[0][1]
    idx = 0
    for slot in range(SLOTS_PER_DAY):
        while idx < len(entries) and entries[idx][0] == slot:
            current = entries[idx][1]
            idx += 1
        day.append(str(current))
    return day


def format_day(slots: Sequence[str]) -> str:
    """Compact a 48-slot day to ``"00:00 sleep, 06:00 home, 23:30 sleep"``."""
    if len(slots) != SLOTS_PER_DAY:
        raise ProgramError(f"Day has {len(slots)} slots, expected {SLOTS_PER_DAY}")
    parts: list[str] = []
    previous: str | None = None
    for slot, ref in enumerate(slots):
        if ref != previous:
            parts.append(f"{slot_to_time(slot)} {ref}")
            previous = ref
    return ", ".join(parts)


def format_schedule(schedule: Sequence[Sequence[str]]) -> dict[str, str]:
    """Return ``{"mon": "00:00 sleep, 06:00 home, …", …}`` for a 7x48 schedule."""
    if len(schedule) != DAYS_PER_WEEK:
        raise ProgramError(f"Schedule has {len(schedule)} days, expected {DAYS_PER_WEEK}")
    return {day: format_day(schedule[i]) for i, day in enumerate(SCHEDULE_DAYS)}


def apply_schedule_spec(
    program: Mapping[str, Any], spec: Mapping[str, str]
) -> list[list[str]]:
    """Return a new 7x48 schedule with the days in ``spec`` replaced.

    ``spec`` maps day/group keys to compact day specs. Unspecified days are untouched.
    Rule for the gap before a day's first entry: it inherits the climate running at the
    end of the previous day (Sunday for Monday), *after* that day's own changes are
    applied, so "weekdays: 06:00 home, 22:00 sleep" leaves sleep running past midnight.
    Climate tokens may be refs or names and are stored as refs.
    """
    current = program.get("schedule")
    if not isinstance(current, list) or len(current) != DAYS_PER_WEEK:
        raise ProgramError("Current program has no valid 7-day schedule")
    schedule: list[list[str]] = [list(day) for day in current]
    for day in schedule:
        if len(day) != SLOTS_PER_DAY:
            raise ProgramError("Current program schedule day is not 48 slots")

    if not spec:
        raise ProgramError("Schedule must specify at least one day")
    day_sources = expand_day_keys(spec.keys())
    # Normalise spec keys so lookups by canonical key work for aliases.
    canonical_spec = {normalize_day_key(k): v for k, v in spec.items()}

    parsed: dict[str, list[tuple[int, str]]] = {}
    for day, source in day_sources.items():
        entries = parse_day_spec(canonical_spec[source])
        parsed[day] = [
            (slot, find_climate(program, token)["climateRef"]) for slot, token in entries
        ]

    # Two passes: the second pass fixes Monday's leading gap when Sunday also changed,
    # and in general lets every day see its predecessor's final state.
    for _ in range(2):
        for day in SCHEDULE_DAYS:
            if day not in parsed:
                continue
            i = DAY_INDEX[day]
            fill = schedule[(i - 1) % DAYS_PER_WEEK][-1]
            schedule[i] = expand_day(parsed[day], fill)
    return schedule


# --------------------------------------------------------------- payload building


def strip_program(program: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy a program and drop read-only ``currentClimateRef``."""
    out = copy.deepcopy(dict(program))
    out.pop("currentClimateRef", None)
    if "climates" not in out or "schedule" not in out:
        raise ProgramError("Program must contain 'climates' and 'schedule'")
    return out


def with_comfort_setting(
    program: Mapping[str, Any],
    climate_ref: str,
    *,
    heat_tenths: int | None = None,
    cool_tenths: int | None = None,
    sensors: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a complete program copy with one climate's temps/sensors changed."""
    if heat_tenths is None and cool_tenths is None and sensors is None:
        raise ProgramError("Nothing to change: give heat_temp, cool_temp and/or sensors")
    out = strip_program(program)
    target = find_climate(out, climate_ref)
    if heat_tenths is not None:
        target["heatTemp"] = int(heat_tenths)
    if cool_tenths is not None:
        target["coolTemp"] = int(cool_tenths)
    if sensors is not None:
        target["sensors"] = [dict(s) for s in sensors]
    return out


def with_schedule(
    program: Mapping[str, Any], spec: Mapping[str, str]
) -> dict[str, Any]:
    """Return a complete program copy with the schedule spec applied."""
    out = strip_program(program)
    out["schedule"] = apply_schedule_spec(out, spec)
    return out


def match_climates(
    source: Mapping[str, Any], target: Mapping[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pair every source climate with the target climate it corresponds to (or None).

    Standard refs (:data:`STANDARD_CLIMATE_REFS`) match by ``climateRef``. Any other ref
    is server-generated per thermostat, so custom climates match by name
    (case-insensitive) and keep the TARGET's ``climateRef``. A same-ref climate with a
    different name is NOT a match (``smart1`` = "Office" on the source and "Nursery" on
    the target are unrelated). Each target climate is matched at most once.
    """
    tgt_climates = list(target.get("climates", []))
    by_ref = {c["climateRef"]: c for c in tgt_climates if c.get("climateRef")}
    by_name: dict[str, dict[str, Any]] = {}
    for climate in tgt_climates:
        by_name.setdefault(str(climate.get("name", "")).strip().casefold(), climate)
    taken: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for src_climate in source.get("climates", []):
        ref = src_climate.get("climateRef")
        name = str(src_climate.get("name", "")).strip().casefold()
        match: dict[str, Any] | None = None
        candidate = by_ref.get(ref) if ref else None
        if candidate is not None and (
            ref in STANDARD_CLIMATE_REFS
            or str(candidate.get("name", "")).strip().casefold() == name
        ):
            match = candidate
        elif name:
            match = by_name.get(name)
        if match is not None and id(match) in taken:
            match = None
        if match is not None:
            taken.add(id(match))
        pairs.append((src_climate, match))
    return pairs


def climate_ref_map(
    source: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, str]:
    """Return ``{source_ref: target_ref}`` for every matched climate pair."""
    return {
        src["climateRef"]: tgt["climateRef"]
        for src, tgt in match_climates(source, target)
        if tgt is not None and src.get("climateRef") and tgt.get("climateRef")
    }


def unmatched_climates(source: Mapping[str, Any], target: Mapping[str, Any]) -> list[str]:
    """Names of source climates that have no counterpart on the target yet."""
    return [
        str(src.get("name", src.get("climateRef")))
        for src, tgt in match_climates(source, target)
        if tgt is None
    ]


def copy_program(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    comfort_settings: bool = True,
    schedule: bool = True,
    preserve_target_temperatures: bool = False,
    default_sensors: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Return the complete program to write to ``target`` after copying from ``source``.

    Semantics:

    * Climates are paired with :func:`match_climates`: standard refs by climateRef,
      custom climates by name (the target keeps its own climateRef).
    * ``comfort_settings``: matched climates take all source fields except
      ``climateRef`` (kept) and ``sensors`` (the target keeps its own — sensors are
      thermostat-specific). With ``preserve_target_temperatures`` the target's
      heatTemp/coolTemp are kept too. Source climates with no match are appended WITHOUT
      ``climateRef`` (the server generates one) and get ``default_sensors``; if that is
      None they inherit the target's "home" sensors, else the first target climate that
      has sensors; if nothing has sensors ProgramError. Target-only climates are kept.
    * ``schedule``: the source's 7x48 schedule replaces the target's, with every slot
      remapped through the climate pairing (source ``smart1`` -> target ``smart3`` when
      both are "Office"). A slot whose climate has no target counterpart keeps the source
      ref and fails :func:`validate_program`; the caller must create the climate first
      (``comfort_settings`` only), refresh, then copy the schedule.
    """
    if not comfort_settings and not schedule:
        raise ProgramError("Nothing to copy: enable comfort_settings and/or schedule")
    out = strip_program(target)
    src = strip_program(source)
    pairs = match_climates(src, out)
    ref_map = {
        s["climateRef"]: t["climateRef"]
        for s, t in pairs
        if t is not None and s.get("climateRef") and t.get("climateRef")
    }

    if comfort_settings:
        fallback_sensors: list[dict[str, str]] | None = None
        if default_sensors is not None:
            fallback_sensors = [dict(s) for s in default_sensors]
        else:
            home = next(
                (c for c in out["climates"] if c.get("climateRef") == "home"), None
            )
            donor = home if home and home.get("sensors") else None
            if donor is None:
                donor = next((c for c in out["climates"] if c.get("sensors")), None)
            if donor is not None:
                fallback_sensors = copy.deepcopy(donor["sensors"])
        for src_climate, existing in pairs:
            if existing is not None:
                keep_sensors = existing.get("sensors", [])
                keep_heat = existing.get("heatTemp")
                keep_cool = existing.get("coolTemp")
                for key, value in src_climate.items():
                    if key in ("climateRef", "sensors"):
                        continue
                    existing[key] = copy.deepcopy(value)
                existing["sensors"] = keep_sensors
                if preserve_target_temperatures:
                    if keep_heat is not None:
                        existing["heatTemp"] = keep_heat
                    if keep_cool is not None:
                        existing["coolTemp"] = keep_cool
            else:
                if not fallback_sensors:
                    raise ProgramError(
                        f"Cannot create climate {src_climate.get('name')!r} on the "
                        "target: no target climate has sensors to inherit"
                    )
                new_climate = {
                    k: copy.deepcopy(v)
                    for k, v in src_climate.items()
                    if k not in ("climateRef", "sensors")
                }
                new_climate["sensors"] = copy.deepcopy(fallback_sensors)
                out["climates"].append(new_climate)

    if schedule:
        src_schedule = src["schedule"]
        if len(src_schedule) != DAYS_PER_WEEK or any(
            len(d) != SLOTS_PER_DAY for d in src_schedule
        ):
            raise ProgramError("Source program schedule is not 7x48")
        out["schedule"] = [[ref_map.get(ref, ref) for ref in day] for day in src_schedule]

    return out


# -------------------------------------------------------------------- validation


def deadband_for(thermostat: Mapping[str, Any]) -> int:
    """Return the thermostat's minimum cool-heat gap in tenths °F.

    ``settings.heatCoolMinDelta`` is what the server enforces in auto mode (factory
    default 50 = 5 °F); fall back to :data:`DEFAULT_DEADBAND_TENTHS` when missing.
    """
    value = (thermostat.get("settings") or {}).get("heatCoolMinDelta")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return DEFAULT_DEADBAND_TENTHS


def temp_ranges_for(
    thermostat: Mapping[str, Any],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``((heat_low, heat_high), (cool_low, cool_high))`` in tenths °F.

    Read from ``settings.heatRangeLow/High`` and ``coolRangeLow/High`` (the limits the
    thermostat UI itself enforces); each missing or nonsensical bound falls back to the
    45–92 °F constants.
    """
    settings = thermostat.get("settings") or {}

    def _bound(key: str, default: int) -> int:
        value = settings.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return default

    def _range(prefix: str) -> tuple[int, int]:
        low = _bound(f"{prefix}RangeLow", MIN_TEMP_TENTHS)
        high = _bound(f"{prefix}RangeHigh", MAX_TEMP_TENTHS)
        if low >= high:
            return (MIN_TEMP_TENTHS, MAX_TEMP_TENTHS)
        return (low, high)

    return _range("heat"), _range("cool")


def validate_program(
    program: Mapping[str, Any],
    *,
    hvac_mode: str | None = None,
    deadband_tenths: int = DEFAULT_DEADBAND_TENTHS,
    heat_range: tuple[int, int] = (MIN_TEMP_TENTHS, MAX_TEMP_TENTHS),
    cool_range: tuple[int, int] = (MIN_TEMP_TENTHS, MAX_TEMP_TENTHS),
) -> None:
    """Raise ProgramError if ``program`` must not be sent to the thermostat.

    Checks: 7x48 schedule whose refs all exist; unique climate names (case-insensitive)
    and unique climateRefs; every climate has at least one sensor; heatTemp within
    ``heat_range`` and coolTemp within ``cool_range`` (tenths °F, default 45–92 °F —
    pass :func:`temp_ranges_for`); cool >= heat + ``deadband_tenths`` (pass
    :func:`deadband_for`) unless ``hvac_mode`` is in :data:`SINGLE_MODE_HVAC`. New
    climates (no climateRef) may only be referenced by the schedule via a name match,
    which the server does not do — so the schedule may only reference refs that exist.
    """
    climates = program.get("climates")
    schedule = program.get("schedule")
    if not isinstance(climates, list) or not climates:
        raise ProgramError("Program has no climates")
    if not isinstance(schedule, list) or len(schedule) != DAYS_PER_WEEK:
        raise ProgramError("Program schedule must have 7 days")

    refs: set[str] = set()
    names: set[str] = set()
    for climate in climates:
        name = str(climate.get("name", "")).strip()
        if not name:
            raise ProgramError("Every climate needs a name")
        if name.casefold() in names:
            raise ProgramError(f"Duplicate climate name {name!r}")
        names.add(name.casefold())
        ref = climate.get("climateRef")
        if ref is not None:
            if ref in refs:
                raise ProgramError(f"Duplicate climateRef {ref!r}")
            refs.add(ref)
        if "sensors" in climate and not climate["sensors"]:
            raise ProgramError(f"Climate {name!r} needs at least one sensor")
        for key, (low, high) in (("heatTemp", heat_range), ("coolTemp", cool_range)):
            value = climate.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ProgramError(f"Climate {name!r} {key} must be an integer in tenths °F")
            if not low <= value <= high:
                raise ProgramError(
                    f"Climate {name!r} {key} {value / 10:.1f} °F is outside the "
                    f"thermostat's {low / 10:g}–{high / 10:g} °F range"
                )
        if hvac_mode not in SINGLE_MODE_HVAC:
            if climate["coolTemp"] < climate["heatTemp"] + deadband_tenths:
                raise ProgramError(
                    f"Climate {name!r}: cool {climate['coolTemp'] / 10:.1f} °F must be at "
                    f"least {deadband_tenths / 10:.1f} °F above heat "
                    f"{climate['heatTemp'] / 10:.1f} °F"
                )

    for i, day in enumerate(schedule):
        if not isinstance(day, list) or len(day) != SLOTS_PER_DAY:
            raise ProgramError(f"Schedule day {SCHEDULE_DAYS[i]} must have 48 slots")
        for slot, ref in enumerate(day):
            if ref not in refs:
                raise ProgramError(
                    f"Schedule {SCHEDULE_DAYS[i]} {slot_to_time(slot)} references unknown "
                    f"climate {ref!r}"
                )


# ----------------------------------------------------------------------- summary


def program_summary(
    program: Mapping[str, Any], unit: str = UNIT_FAHRENHEIT
) -> dict[str, Any]:
    """Return the ``get_program`` response shape in the user's unit."""
    climates = [
        {
            "ref": c.get("climateRef"),
            "name": c.get("name"),
            "heat_temp": tenths_to_temp(c["heatTemp"], unit),
            "cool_temp": tenths_to_temp(c["coolTemp"], unit),
            "sensors": [s.get("name") or s.get("id") for s in c.get("sensors", [])],
        }
        for c in program.get("climates", [])
    ]
    return {
        "climates": climates,
        "schedule": format_schedule(program["schedule"]),
        "current_climate_ref": program.get("currentClimateRef"),
    }
