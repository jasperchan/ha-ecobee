"""Unit tests for custom_components/ecobee/program.py (plain pytest, no HA)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "ecobee" / "program.py"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "thermostats_program.json"


def _load_program_module():
    # Load program.py directly: importing the package would pull in HA and pyecobee.
    spec = importlib.util.spec_from_file_location("ecobee_program", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


program = _load_program_module()
ProgramError = program.ProgramError


@pytest.fixture(scope="module")
def thermostats() -> list[dict]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def atlas(thermostats) -> dict:
    """Atlas Room (Home 70/78, two sensors)."""
    return copy.deepcopy(thermostats[0])


@pytest.fixture
def hallway(thermostats) -> dict:
    """Hallway (Home 69/78, two sensors)."""
    return copy.deepcopy(thermostats[1])


@pytest.fixture
def kitchen(thermostats) -> dict:
    """Kitchen (Home 69/78, one sensor)."""
    return copy.deepcopy(thermostats[4])


# ------------------------------------------------------------------ constants


def test_day_order_is_monday_first():
    # Documented in program.py from ecobee API example 11. Regression guard so a change
    # of the constant is deliberate.
    assert program.SCHEDULE_DAYS == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert program.DAY_INDEX["mon"] == 0
    assert program.DAY_INDEX["sun"] == 6
    assert program.DAY_GROUPS["weekend"] == ("sat", "sun")
    assert len(program.DAY_GROUPS["all"]) == 7


# --------------------------------------------------------------- temperatures


@pytest.mark.parametrize(
    ("value", "unit", "tenths"),
    [
        (70, "°F", 700),
        (70.5, "°F", 705),
        (64.0, "°F", 640),
        (21.0, "°C", 698),  # 69.8 °F
        (20.0, "°C", 680),
        (0.0, "°C", 320),
    ],
)
def test_temp_to_tenths(value, unit, tenths):
    assert program.temp_to_tenths(value, unit) == tenths


@pytest.mark.parametrize(
    ("tenths", "unit", "value"),
    [(700, "°F", 70.0), (705, "°F", 70.5), (680, "°C", 20.0), (698, "°C", 21.0)],
)
def test_tenths_to_temp(tenths, unit, value):
    assert program.tenths_to_temp(tenths, unit) == value


def test_temp_round_trip_fahrenheit_half_degrees():
    for tenths in range(450, 925, 5):
        assert program.temp_to_tenths(program.tenths_to_temp(tenths)) == tenths


def test_temp_unknown_unit():
    with pytest.raises(ProgramError):
        program.temp_to_tenths(70, "K")
    with pytest.raises(ProgramError):
        program.tenths_to_temp(700, "K")


# ------------------------------------------------------------------- times


@pytest.mark.parametrize(
    ("text", "slot"),
    [
        ("00:00", 0),
        ("00:30", 1),
        ("07:00", 14),
        ("7:00", 14),
        ("07:10", 14),  # snaps down
        ("07:20", 15),  # snaps up
        # Exact quarter-hour ties round half-up (round() is half-to-even and sent
        # 06:45 and 07:15 to the same slot).
        ("00:15", 1),
        ("00:45", 2),
        ("06:45", 14),
        ("07:15", 15),
        ("07:45", 16),
        ("23:30", 47),
        ("23:50", 47),  # never past the end of the day
    ],
)
def test_parse_time_to_slot(text, slot):
    assert program.parse_time_to_slot(text) == slot


def test_parse_day_spec_quarter_hour_ties_stay_increasing():
    assert program.parse_day_spec("06:45 home, 07:15 sleep") == [(14, "home"), (15, "sleep")]


@pytest.mark.parametrize("text", ["24:00", "7", "07:60", "seven", "", "7:0"])
def test_parse_time_invalid(text):
    with pytest.raises(ProgramError):
        program.parse_time_to_slot(text)


def test_slot_to_time():
    assert program.slot_to_time(0) == "00:00"
    assert program.slot_to_time(14) == "07:00"
    assert program.slot_to_time(47) == "23:30"
    with pytest.raises(ProgramError):
        program.slot_to_time(48)


# --------------------------------------------------------------- day specs


def test_parse_day_spec():
    assert program.parse_day_spec("07:00 home, 21:00 sleep") == [(14, "home"), (42, "sleep")]
    assert program.parse_day_spec("00:00 Sleep; 6:10 Home") == [(0, "Sleep"), (12, "Home")]
    assert program.parse_day_spec(" 08:00 away ,") == [(16, "away")]


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "home",
        "07:00",
        "07:00 home, 07:10 sleep",  # same slot after snapping
        "21:00 sleep, 07:00 home",  # not increasing
        "25:00 home",
    ],
)
def test_parse_day_spec_invalid(spec):
    with pytest.raises(ProgramError):
        program.parse_day_spec(spec)


def test_expand_day_from_midnight():
    day = program.expand_day([(0, "sleep"), (12, "home"), (44, "sleep")], None)
    assert len(day) == 48
    assert day[:12] == ["sleep"] * 12
    assert day[12:44] == ["home"] * 32
    assert day[44:] == ["sleep"] * 4


def test_expand_day_inherits_fill_before_first_entry():
    day = program.expand_day([(14, "home"), (42, "sleep")], "away")
    assert day[:14] == ["away"] * 14
    assert day[14] == "home"
    assert day[42:] == ["sleep"] * 6


def test_expand_day_requires_fill_when_not_starting_at_midnight():
    with pytest.raises(ProgramError):
        program.expand_day([(14, "home")], None)
    with pytest.raises(ProgramError):
        program.expand_day([], "home")


def test_format_day_and_round_trip(atlas):
    monday = atlas["program"]["schedule"][0]
    text = program.format_day(monday)
    assert text == "00:00 sleep, 06:00 home, 23:30 sleep"
    rebuilt = program.expand_day(program.parse_day_spec(text), None)
    assert rebuilt == monday


def test_format_day_wrong_length():
    with pytest.raises(ProgramError):
        program.format_day(["home"] * 47)


def test_format_schedule(hallway):
    formatted = program.format_schedule(hallway["program"]["schedule"])
    assert list(formatted) == list(program.SCHEDULE_DAYS)
    assert formatted["mon"] == "00:00 sleep, 06:30 home, 23:30 sleep"
    assert formatted["sun"] == formatted["mon"]
    with pytest.raises(ProgramError):
        program.format_schedule(hallway["program"]["schedule"][:6])


# ---------------------------------------------------------------- day keys


def test_normalize_day_key():
    assert program.normalize_day_key("Mon") == "mon"
    assert program.normalize_day_key("sunday") == "sun"
    assert program.normalize_day_key("Weekends") == "weekend"
    assert program.normalize_day_key("everyday") == "all"
    with pytest.raises(ProgramError):
        program.normalize_day_key("someday")


def test_expand_day_keys_groups_and_overrides():
    result = program.expand_day_keys(["weekdays", "weekend"])
    assert set(result) == set(program.SCHEDULE_DAYS)
    assert result["mon"] == "weekdays"
    assert result["sat"] == "weekend"

    result = program.expand_day_keys(["all", "weekend", "fri"])
    assert result["mon"] == "all"
    assert result["sat"] == "weekend"
    assert result["fri"] == "fri"


def test_expand_day_keys_duplicate_same_rank():
    with pytest.raises(ProgramError):
        program.expand_day_keys(["mon", "monday"])
    with pytest.raises(ProgramError):
        program.expand_day_keys(["weekdays", "weekday"])


# ------------------------------------------------------- apply_schedule_spec


def test_apply_schedule_spec_untouched_days_and_inherit_gap(atlas):
    prog = atlas["program"]
    before = copy.deepcopy(prog["schedule"])
    new = program.apply_schedule_spec(prog, {"weekdays": "07:00 home, 21:00 sleep"})
    # Monday inherits Sunday's last slot (sleep) for 00:00-07:00.
    assert new[0][:14] == ["sleep"] * 14
    assert new[0][14:42] == ["home"] * 28
    assert new[0][42:] == ["sleep"] * 6
    for i in range(1, 5):
        assert new[i] == new[0]
    # Weekend untouched.
    assert new[5] == before[5]
    assert new[6] == before[6]
    # Input not mutated.
    assert prog["schedule"] == before


def test_apply_schedule_spec_monday_sees_new_sunday(atlas):
    prog = atlas["program"]
    new = program.apply_schedule_spec(
        prog, {"sun": "00:00 home, 20:00 away", "mon": "06:00 home, 22:00 sleep"}
    )
    assert new[6][-1] == "away"
    assert new[0][:12] == ["away"] * 12  # Monday 00:00-06:00 inherits Sunday's away
    assert new[0][12] == "home"


def test_apply_schedule_spec_names_resolve_to_refs(atlas):
    new = program.apply_schedule_spec(atlas["program"], {"sat": "00:00 Sleep, 09:00 HOME"})
    assert new[5][0] == "sleep"
    assert new[5][18] == "home"


def test_apply_schedule_spec_errors(atlas):
    with pytest.raises(ProgramError):
        program.apply_schedule_spec(atlas["program"], {})
    with pytest.raises(ProgramError):
        program.apply_schedule_spec(atlas["program"], {"mon": "00:00 office"})
    with pytest.raises(ProgramError):
        program.apply_schedule_spec(atlas["program"], {"funday": "00:00 home"})
    broken = {"climates": atlas["program"]["climates"], "schedule": [["home"] * 48] * 6}
    with pytest.raises(ProgramError):
        program.apply_schedule_spec(broken, {"mon": "00:00 home"})


def test_with_schedule_strips_current_climate_ref(atlas):
    out = program.with_schedule(atlas["program"], {"all": "00:00 home"})
    assert "currentClimateRef" not in out
    assert all(day == ["home"] * 48 for day in out["schedule"])
    assert out["climates"] == atlas["program"]["climates"]


# ------------------------------------------------------------ comfort setting


def test_find_climate(atlas):
    prog = atlas["program"]
    assert program.find_climate(prog, "home")["name"] == "Home"
    assert program.find_climate(prog, "Home")["climateRef"] == "home"
    assert program.find_climate(prog, "SLEEP")["climateRef"] == "sleep"
    with pytest.raises(ProgramError, match="Unknown climate"):
        program.find_climate(prog, "office")
    assert program.climate_refs(prog) == ["away", "sleep", "home"]


def test_with_comfort_setting_temps(atlas):
    prog = atlas["program"]
    out = program.with_comfort_setting(prog, "home", heat_tenths=680, cool_tenths=760)
    home = program.find_climate(out, "home")
    assert home["heatTemp"] == 680
    assert home["coolTemp"] == 760
    # Everything else intact, full program present, read-only field stripped.
    assert "currentClimateRef" not in out
    assert len(out["climates"]) == 3
    assert program.find_climate(out, "away")["heatTemp"] == 640
    assert out["schedule"] == prog["schedule"]
    assert home["sensors"] == program.find_climate(prog, "home")["sensors"]
    # Source untouched.
    assert program.find_climate(prog, "home")["heatTemp"] == 700


def test_with_comfort_setting_sensors_only(atlas):
    sensors = program.resolve_sensors(["Jasper Room"], atlas["remoteSensors"])
    out = program.with_comfort_setting(atlas["program"], "sleep", sensors=sensors)
    assert program.find_climate(out, "sleep")["sensors"] == [
        {"id": "rs2:100:1", "name": "Jasper Room"}
    ]
    assert program.find_climate(out, "sleep")["heatTemp"] == 650


def test_with_comfort_setting_errors(atlas):
    with pytest.raises(ProgramError, match="Nothing to change"):
        program.with_comfort_setting(atlas["program"], "home")
    with pytest.raises(ProgramError, match="Unknown climate"):
        program.with_comfort_setting(atlas["program"], "office", heat_tenths=700)


def test_resolve_sensors(atlas):
    remote = atlas["remoteSensors"]
    assert program.resolve_sensors(["atlas room"], remote) == [{"id": "ei:0:1", "name": "Atlas Room"}]
    assert program.resolve_sensors(["ei:0"], remote) == [{"id": "ei:0:1", "name": "Atlas Room"}]
    assert program.resolve_sensors(["rs2:100:1"], remote) == [
        {"id": "rs2:100:1", "name": "Jasper Room"}
    ]
    # Mixed, de-duplicated, order kept.
    assert [s["id"] for s in program.resolve_sensors(["Jasper Room", "ei:0", "rs2:100"], remote)] == [
        "rs2:100:1",
        "ei:0:1",
    ]
    with pytest.raises(ProgramError, match="Unknown sensor"):
        program.resolve_sensors(["Garage"], remote)
    with pytest.raises(ProgramError):
        program.resolve_sensors([], remote)


# --------------------------------------------------------------- validation


def test_validate_fixture_programs(thermostats):
    for thermostat in thermostats:
        program.validate_program(program.strip_program(thermostat["program"]), hvac_mode="auto")


def test_validate_temperature_bounds(atlas):
    low = program.with_comfort_setting(atlas["program"], "away", heat_tenths=440)
    with pytest.raises(ProgramError, match="outside"):
        program.validate_program(low)
    high = program.with_comfort_setting(atlas["program"], "away", cool_tenths=925)
    with pytest.raises(ProgramError, match="outside"):
        program.validate_program(high)
    edge = program.with_comfort_setting(atlas["program"], "away", heat_tenths=450, cool_tenths=920)
    program.validate_program(edge)


def test_validate_deadband(atlas):
    tight = program.with_comfort_setting(atlas["program"], "home", heat_tenths=700, cool_tenths=715)
    with pytest.raises(ProgramError, match="above heat"):
        program.validate_program(tight, hvac_mode="auto")
    with pytest.raises(ProgramError):
        program.validate_program(tight)  # unknown mode -> enforce
    # Exactly the deadband is fine.
    ok = program.with_comfort_setting(atlas["program"], "home", heat_tenths=700, cool_tenths=720)
    program.validate_program(ok, hvac_mode="auto")
    # Heat-only / cool-only / off thermostats skip the deadband check.
    for mode in ("heat", "cool", "auxHeatOnly", "off"):
        program.validate_program(tight, hvac_mode=mode)
    # Custom deadband.
    with pytest.raises(ProgramError):
        program.validate_program(ok, hvac_mode="auto", deadband_tenths=50)


def test_deadband_for_reads_heat_cool_min_delta(atlas):
    assert program.deadband_for(atlas) == program.DEFAULT_DEADBAND_TENTHS  # no settings
    atlas["settings"] = {"hvacMode": "auto", "heatCoolMinDelta": 50}
    assert program.deadband_for(atlas) == 50
    atlas["settings"]["heatCoolMinDelta"] = "50"  # garbage -> default
    assert program.deadband_for(atlas) == program.DEFAULT_DEADBAND_TENTHS
    # Thermostat with the factory 5 °F delta: 75/77 is rejected, 75/80 passes.
    atlas["settings"]["heatCoolMinDelta"] = 50
    tight = program.with_comfort_setting(atlas["program"], "home", heat_tenths=750, cool_tenths=770)
    with pytest.raises(ProgramError, match="5.0 °F above"):
        program.validate_program(tight, hvac_mode="auto", deadband_tenths=program.deadband_for(atlas))
    ok = program.with_comfort_setting(atlas["program"], "home", heat_tenths=750, cool_tenths=800)
    program.validate_program(ok, hvac_mode="auto", deadband_tenths=program.deadband_for(atlas))


def test_temp_ranges_for_and_per_field_bounds(atlas):
    default = ((450, 920), (450, 920))
    assert program.temp_ranges_for(atlas) == default
    atlas["settings"] = {
        "heatRangeLow": 450,
        "heatRangeHigh": 790,
        "coolRangeLow": 650,
        "coolRangeHigh": 920,
    }
    heat_range, cool_range = program.temp_ranges_for(atlas)
    assert (heat_range, cool_range) == ((450, 790), (650, 920))
    # Inverted or missing bounds fall back to the constants.
    assert program.temp_ranges_for({"settings": {"heatRangeLow": 800, "heatRangeHigh": 700}}) == default
    # Away heat 85 °F is inside 45–92 but outside the heat range; cool 55 outside cool range.
    hot = program.with_comfort_setting(atlas["program"], "away", heat_tenths=850)
    program.validate_program(hot, hvac_mode="heat")  # default bounds accept it
    with pytest.raises(ProgramError, match="heatTemp 85.0 °F is outside the thermostat's 45–79"):
        program.validate_program(hot, hvac_mode="heat", heat_range=heat_range, cool_range=cool_range)
    cold = program.with_comfort_setting(atlas["program"], "away", heat_tenths=500, cool_tenths=550)
    with pytest.raises(ProgramError, match="coolTemp 55.0 °F is outside the thermostat's 65–92"):
        program.validate_program(cold, heat_range=heat_range, cool_range=cool_range)


def test_validate_rejects_empty_sensors(atlas):
    bad = program.strip_program(atlas["program"])
    bad["climates"][0]["sensors"] = []
    with pytest.raises(ProgramError, match="at least one sensor"):
        program.validate_program(bad)


def test_validate_duplicate_names_and_refs(atlas):
    dup = program.strip_program(atlas["program"])
    dup["climates"][0]["name"] = "home"  # case-insensitive clash with "Home"
    with pytest.raises(ProgramError, match="Duplicate climate name"):
        program.validate_program(dup)
    dup = program.strip_program(atlas["program"])
    dup["climates"][0]["climateRef"] = "home"
    with pytest.raises(ProgramError, match="Duplicate climateRef"):
        program.validate_program(dup)


def test_validate_schedule_refs_and_shape(atlas):
    bad = program.strip_program(atlas["program"])
    bad["schedule"][3][10] = "office"
    with pytest.raises(ProgramError, match="thu 05:00"):
        program.validate_program(bad)
    short = program.strip_program(atlas["program"])
    short["schedule"][0] = short["schedule"][0][:47]
    with pytest.raises(ProgramError, match="48 slots"):
        program.validate_program(short)
    six = program.strip_program(atlas["program"])
    six["schedule"] = six["schedule"][:6]
    with pytest.raises(ProgramError, match="7 days"):
        program.validate_program(six)
    with pytest.raises(ProgramError):
        program.validate_program({"climates": [], "schedule": six["schedule"]})


def test_validate_rejects_non_integer_temps(atlas):
    bad = program.strip_program(atlas["program"])
    bad["climates"][0]["heatTemp"] = 64.0
    with pytest.raises(ProgramError, match="integer"):
        program.validate_program(bad)


def test_strip_program_requires_keys():
    with pytest.raises(ProgramError):
        program.strip_program({"climates": []})


# -------------------------------------------------------------- copy_program


def _by_ref(prog, ref):
    return program.find_climate(prog, ref)


def test_copy_comfort_settings_keeps_target_sensors_and_refs(atlas, kitchen):
    out = program.copy_program(atlas["program"], kitchen["program"], schedule=False)
    home = _by_ref(out, "home")
    assert home["heatTemp"] == 700  # Atlas value copied
    assert home["coolTemp"] == 780
    assert home["climateRef"] == "home"
    # Kitchen keeps its own single sensor; Atlas's two are not copied.
    assert home["sensors"] == [{"id": "ei:0:1", "name": "Kitchen"}]
    # Schedule not copied.
    assert out["schedule"] == kitchen["program"]["schedule"]
    assert "currentClimateRef" not in out
    # Source and target inputs untouched.
    assert _by_ref(kitchen["program"], "home")["heatTemp"] == 690
    program.validate_program(out, hvac_mode="auto")


def test_copy_schedule_only(atlas, kitchen):
    out = program.copy_program(atlas["program"], kitchen["program"], comfort_settings=False)
    assert out["schedule"] == atlas["program"]["schedule"]
    assert out["schedule"] is not atlas["program"]["schedule"]
    assert _by_ref(out, "home")["heatTemp"] == 690  # Kitchen temps untouched
    program.validate_program(out, hvac_mode="auto")


def test_copy_preserve_target_temperatures(atlas, kitchen):
    src = atlas["program"]
    _by_ref(src, "home")["coolFan"] = "on"
    out = program.copy_program(src, kitchen["program"], preserve_target_temperatures=True)
    home = _by_ref(out, "home")
    assert home["heatTemp"] == 690
    assert home["coolTemp"] == 780
    assert home["coolFan"] == "on"  # non-temperature fields still copied
    assert out["schedule"] == src["schedule"]


def test_copy_creates_missing_climate_without_ref(atlas, kitchen):
    src = program.strip_program(atlas["program"])
    src["climates"].append(
        {
            "name": "Office",
            "climateRef": "smart1",
            "isOccupied": True,
            "isOptimized": False,
            "coolFan": "auto",
            "heatFan": "auto",
            "vent": "off",
            "ventilatorMinOnTime": 20,
            "owner": "user",
            "type": "program",
            "colour": 1,
            "coolTemp": 760,
            "heatTemp": 690,
            "sensors": [{"id": "rs2:100:1", "name": "Jasper Room"}],
        }
    )
    out = program.copy_program(src, kitchen["program"], schedule=False)
    office = program.find_climate(out, "Office")
    assert "climateRef" not in office
    assert office["heatTemp"] == 690
    # Sensors are not copied; new climate inherits the target's Home sensors.
    assert office["sensors"] == [{"id": "ei:0:1", "name": "Kitchen"}]
    assert len(out["climates"]) == 4
    # Explicit default sensors win.
    out2 = program.copy_program(
        src, kitchen["program"], schedule=False, default_sensors=[{"id": "ei:0:1"}]
    )
    assert program.find_climate(out2, "Office")["sensors"] == [{"id": "ei:0:1"}]


def test_copy_keeps_target_only_climates(atlas, kitchen):
    tgt = program.strip_program(kitchen["program"])
    tgt["climates"].append(dict(tgt["climates"][0], name="Guests", climateRef="smart2"))
    out = program.copy_program(atlas["program"], tgt)
    assert program.find_climate(out, "smart2")["name"] == "Guests"
    assert len(out["climates"]) == 4


def test_copy_schedule_with_unknown_ref_fails_validation(atlas, kitchen):
    src = program.strip_program(atlas["program"])
    src["climates"].append(dict(src["climates"][0], name="Office", climateRef="smart1"))
    src["schedule"][0][20] = "smart1"
    # Schedule only: the target has no smart1 -> invalid program.
    out = program.copy_program(src, kitchen["program"], comfort_settings=False)
    with pytest.raises(ProgramError, match="unknown climate 'smart1'"):
        program.validate_program(out)
    # With comfort settings the new climate has no climateRef yet, so the schedule still
    # cannot reference it on the first write; validation must say so rather than let the
    # server reject it.
    out = program.copy_program(src, kitchen["program"])
    with pytest.raises(ProgramError, match="unknown climate 'smart1'"):
        program.validate_program(out)


def _custom(base, name, ref, heat, cool):
    return dict(base, name=name, climateRef=ref, heatTemp=heat, coolTemp=cool)


def test_copy_custom_climate_same_ref_different_name_is_not_merged(atlas, kitchen):
    """Source smart1=Gym, target smart1=Nursery: Nursery must survive, Gym is appended."""
    src = program.strip_program(atlas["program"])
    src["climates"].append(_custom(src["climates"][0], "Gym", "smart1", 680, 760))
    tgt = program.strip_program(kitchen["program"])
    tgt["climates"].append(_custom(tgt["climates"][0], "Nursery", "smart1", 720, 740))
    out = program.copy_program(src, tgt, schedule=False)
    nursery = program.find_climate(out, "smart1")
    assert (nursery["name"], nursery["heatTemp"], nursery["coolTemp"]) == ("Nursery", 720, 740)
    gym = program.find_climate(out, "Gym")
    assert "climateRef" not in gym
    assert (gym["heatTemp"], gym["coolTemp"]) == (680, 760)
    assert gym["sensors"] == [{"id": "ei:0:1", "name": "Kitchen"}]
    assert len(out["climates"]) == 5
    program.validate_program(out, hvac_mode="auto")
    assert program.unmatched_climates(src, tgt) == ["Gym"]


def test_copy_custom_climate_matches_by_name_and_remaps_schedule(atlas, kitchen):
    """Source smart1=Office on the schedule, target smart2=Office: update in place, remap."""
    src = program.strip_program(atlas["program"])
    src["climates"].append(_custom(src["climates"][0], "Office", "smart1", 690, 760))
    src["schedule"][0][20] = "smart1"
    tgt = program.strip_program(kitchen["program"])
    tgt["climates"].append(_custom(tgt["climates"][0], "office", "smart2", 700, 770))
    assert program.climate_ref_map(src, tgt) == {
        "away": "away", "sleep": "sleep", "home": "home", "smart1": "smart2"
    }
    assert program.unmatched_climates(src, tgt) == []
    out = program.copy_program(src, tgt)
    assert [c.get("climateRef") for c in out["climates"]] == ["away", "home", "sleep", "smart2"]
    office = program.find_climate(out, "smart2")
    assert (office["name"], office["heatTemp"], office["coolTemp"]) == ("Office", 690, 760)
    assert office["sensors"] == tgt["climates"][3]["sensors"]  # target keeps its sensors
    assert out["schedule"][0][20] == "smart2"
    assert out["schedule"][0][19] == src["schedule"][0][19]
    program.validate_program(out, hvac_mode="auto")
    # Schedule-only copies remap too.
    out = program.copy_program(src, tgt, comfort_settings=False)
    assert out["schedule"][0][20] == "smart2"
    program.validate_program(out, hvac_mode="auto")


def test_copy_two_phase_for_new_scheduled_climate(atlas, kitchen):
    """Phase 1 creates the climate; after the server assigns a ref, phase 2 remaps."""
    src = program.strip_program(atlas["program"])
    src["climates"].append(_custom(src["climates"][0], "Office", "smart1", 690, 760))
    src["schedule"][0][20] = "smart1"
    tgt = program.strip_program(kitchen["program"])
    assert program.unmatched_climates(src, tgt) == ["Office"]
    phase1 = program.copy_program(src, tgt, schedule=False)
    program.validate_program(phase1, hvac_mode="auto")
    assert "climateRef" not in program.find_climate(phase1, "Office")
    # Simulate the server assigning its own (unrelated) ref on the re-read.
    refreshed = copy.deepcopy(phase1)
    program.find_climate(refreshed, "Office")["climateRef"] = "smart3"
    phase2 = program.copy_program(src, refreshed, comfort_settings=False)
    assert phase2["schedule"][0][20] == "smart3"
    program.validate_program(phase2, hvac_mode="auto")


def test_copy_fallback_sensors_without_home_climate(atlas, kitchen):
    src = program.strip_program(atlas["program"])
    src["climates"].append(_custom(src["climates"][0], "Office", "smart1", 690, 760))
    tgt = program.strip_program(kitchen["program"])
    tgt["climates"] = [c for c in tgt["climates"] if c["climateRef"] != "home"]
    tgt["schedule"] = [["sleep"] * 48 for _ in range(7)]
    out = program.copy_program(src, tgt, schedule=False)
    for name in ("Home", "Office"):
        assert program.find_climate(out, name)["sensors"] == [{"id": "ei:0:1", "name": "Kitchen"}]
    program.validate_program(out, hvac_mode="auto")
    # No target climate has sensors and no default given -> explicit error.
    for climate in tgt["climates"]:
        climate["sensors"] = []
    with pytest.raises(ProgramError, match="no target climate has sensors"):
        program.copy_program(src, tgt, schedule=False)
    out = program.copy_program(src, tgt, schedule=False, default_sensors=[{"id": "ei:0:1"}])
    assert program.find_climate(out, "Office")["sensors"] == [{"id": "ei:0:1"}]


def test_copy_nothing_selected(atlas, kitchen):
    with pytest.raises(ProgramError):
        program.copy_program(atlas["program"], kitchen["program"], comfort_settings=False, schedule=False)


def test_copy_to_all_house_thermostats(thermostats):
    source = thermostats[0]["program"]
    for thermostat in thermostats[1:]:
        out = program.copy_program(source, thermostat["program"])
        program.validate_program(out, hvac_mode="auto")
        assert program.find_climate(out, "home")["heatTemp"] == 700
        assert out["schedule"] == source["schedule"]
        # Each thermostat keeps its own sensors.
        own_ids = {f"{s['id']}:1" for s in thermostat["remoteSensors"]}
        for climate in out["climates"]:
            assert {s["id"] for s in climate["sensors"]} <= own_ids


# ------------------------------------------------------------------ summary


def test_program_summary(atlas):
    summary = program.program_summary(atlas["program"])
    assert summary["current_climate_ref"] == "home"
    assert summary["schedule"]["mon"] == "00:00 sleep, 06:00 home, 23:30 sleep"
    home = next(c for c in summary["climates"] if c["ref"] == "home")
    assert home == {
        "ref": "home",
        "name": "Home",
        "heat_temp": 70.0,
        "cool_temp": 78.0,
        "sensors": ["Atlas Room", "Jasper Room"],
    }
    celsius = program.program_summary(atlas["program"], "°C")
    home_c = next(c for c in celsius["climates"] if c["ref"] == "home")
    assert home_c["heat_temp"] == 21.1
    assert home_c["cool_temp"] == 25.6
