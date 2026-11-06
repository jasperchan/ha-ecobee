"""Program-control services added by the ha-ecobee fork.

Registered from ``services.async_setup_services`` with a single call so the diff to the
core file stays minimal. All program logic lives in ``program.py`` (pure, unit-tested);
this module only resolves entities, converts units and performs the write.

Services (domain ``ecobee``):

* ``set_comfort_setting`` — change one climate's heat/cool temps and/or sensors.
* ``set_schedule`` — replace selected days of the weekly schedule from compact specs.
* ``copy_program`` — copy climates and/or schedule from one thermostat to others.
* ``get_program`` — return climates and compact schedule (response-only).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import probatio

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import program as prog
from .const import DOMAIN, LOGGER
from .program_writer import ProgramBuilder, async_set_program

if TYPE_CHECKING:
    from .climate import Thermostat

ATTR_CLIMATE_REF = "climate_ref"
ATTR_COMFORT_SETTINGS = "comfort_settings"
ATTR_COOL_TEMP = "cool_temp"
ATTR_HEAT_TEMP = "heat_temp"
ATTR_PRESERVE_TARGET_TEMPERATURES = "preserve_target_temperatures"
ATTR_SCHEDULE = "schedule"
ATTR_SENSORS = "sensors"
ATTR_TARGETS = "targets"

SERVICE_COPY_PROGRAM = "copy_program"
SERVICE_GET_PROGRAM = "get_program"
SERVICE_SET_COMFORT_SETTING = "set_comfort_setting"
SERVICE_SET_SCHEDULE = "set_schedule"


def _schedule_spec(value: Any) -> dict[str, str]:
    """Validate the ``schedule`` field: {day_key: "HH:MM ref, …"}."""
    if not isinstance(value, dict) or not value:
        raise probatio.Invalid(
            "schedule must be a mapping of day keys (mon..sun, weekdays, weekend, all) "
            "to 'HH:MM climate, HH:MM climate' strings"
        )
    out: dict[str, str] = {}
    for key, spec in value.items():
        try:
            day = prog.normalize_day_key(str(key))
            prog.parse_day_spec(str(spec))
        except prog.ProgramError as err:
            raise probatio.Invalid(str(err)) from err
        if day in out:
            raise probatio.Invalid(f"Day key {key!r} given twice")
        out[day] = str(spec)
    return out


SET_COMFORT_SETTING_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_ENTITY_ID): cv.entity_id,
        probatio.Required(ATTR_CLIMATE_REF): cv.string,
        probatio.Optional(ATTR_HEAT_TEMP): probatio.Coerce(float),
        probatio.Optional(ATTR_COOL_TEMP): probatio.Coerce(float),
        probatio.Optional(ATTR_SENSORS): probatio.All(cv.ensure_list, [cv.string]),
    }
)

SET_SCHEDULE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_ENTITY_ID): cv.entity_ids,
        probatio.Required(ATTR_SCHEDULE): _schedule_spec,
    }
)

COPY_PROGRAM_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_ENTITY_ID): cv.entity_id,
        probatio.Required(ATTR_TARGETS): cv.entity_ids,
        probatio.Optional(ATTR_COMFORT_SETTINGS, default=True): cv.boolean,
        probatio.Optional(ATTR_SCHEDULE, default=True): cv.boolean,
        probatio.Optional(ATTR_PRESERVE_TARGET_TEMPERATURES, default=False): cv.boolean,
    }
)

GET_PROGRAM_SCHEMA = probatio.Schema({probatio.Required(ATTR_ENTITY_ID): cv.entity_id})


# ------------------------------------------------------------------ helpers


def _thermostat_entities(hass: HomeAssistant) -> list[Thermostat]:
    # Local import: services.py imports this module, so a top-level import would be
    # circular.
    from .services import _async_get_thermostats  # noqa: PLC0415

    return _async_get_thermostats(hass)


def _thermostat_for(hass: HomeAssistant, entity_id: str) -> Thermostat:
    for thermostat in _thermostat_entities(hass):
        if thermostat.entity_id == entity_id:
            return thermostat
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="program_entity_not_found",
        translation_placeholders={"entity_id": entity_id},
    )


def _unit(hass: HomeAssistant) -> str:
    return str(hass.config.units.temperature_unit)


def _raw(thermostat: Thermostat) -> dict[str, Any]:
    """The cached thermostat dict (may be up to 180 s old; writes rebuild from fresh)."""
    return thermostat.data.ecobee.get_thermostat(thermostat.thermostat_index)


def _current_program(thermostat: Thermostat) -> dict[str, Any]:
    return _raw(thermostat)["program"]


def _invalid(err: prog.ProgramError) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_program",
        translation_placeholders={"error": str(err)},
    )


def validate_for(thermostat: Mapping[str, Any], program: dict[str, Any]) -> dict[str, Any]:
    """Validate ``program`` against this thermostat's own settings; return it.

    Uses the thermostat's hvacMode, heatCoolMinDelta and heat/cool ranges so local
    validation matches what the server enforces. ProgramError -> ServiceValidationError.
    """
    heat_range, cool_range = prog.temp_ranges_for(thermostat)
    try:
        prog.validate_program(
            program,
            hvac_mode=(thermostat.get("settings") or {}).get("hvacMode"),
            deadband_tenths=prog.deadband_for(thermostat),
            heat_range=heat_range,
            cool_range=cool_range,
        )
    except prog.ProgramError as err:
        raise _invalid(err) from err
    return program


def _checked(builder: ProgramBuilder) -> ProgramBuilder:
    """Wrap a pure builder so ProgramError becomes ServiceValidationError and the
    result is validated against the (fresh) thermostat it is built from."""

    def build(thermostat: Mapping[str, Any]) -> dict[str, Any]:
        try:
            program = builder(thermostat)
        except prog.ProgramError as err:
            raise _invalid(err) from err
        return validate_for(thermostat, program)

    return build


async def _write(hass: HomeAssistant, thermostat: Thermostat, builder: ProgramBuilder) -> None:
    """Dry-run ``builder`` on the cached data, then write from fresh data.

    The dry run surfaces validation errors before any network call (so a multi-target
    service can check every thermostat first — see ``_write_all``); the real build runs
    inside ``async_set_program`` on the just-refreshed thermostat dict.
    """
    build = _checked(builder)
    build(_raw(thermostat))
    await async_set_program(hass, thermostat.data, thermostat.thermostat_index, build)
    # The climate entity snapshots its thermostat dict in async_update; a plain
    # async_write_ha_state would republish the pre-write state. The throttled
    # data.update() inside returns immediately (the writer just refreshed).
    thermostat.async_schedule_update_ha_state(True)


async def _write_all(
    hass: HomeAssistant, jobs: list[tuple[Thermostat, ProgramBuilder]]
) -> None:
    """Validate every (thermostat, builder) against cached data, then write each.

    Validation failures are therefore atomic: nothing is written if any target fails.
    (A network failure part-way can still leave earlier targets written.)
    """
    checked = [(thermostat, _checked(builder)) for thermostat, builder in jobs]
    for thermostat, build in checked:
        build(_raw(thermostat))
    for thermostat, build in checked:
        await async_set_program(hass, thermostat.data, thermostat.thermostat_index, build)
        thermostat.async_schedule_update_ha_state(True)


# ----------------------------------------------------------------- handlers


async def _set_comfort_setting(call: ServiceCall) -> None:
    hass = call.hass
    thermostat = _thermostat_for(hass, call.data[ATTR_ENTITY_ID])
    unit = _unit(hass)
    heat = call.data.get(ATTR_HEAT_TEMP)
    cool = call.data.get(ATTR_COOL_TEMP)
    try:
        heat_tenths = prog.temp_to_tenths(heat, unit) if heat is not None else None
        cool_tenths = prog.temp_to_tenths(cool, unit) if cool is not None else None
    except prog.ProgramError as err:
        raise _invalid(err) from err
    sensor_tokens = call.data.get(ATTR_SENSORS)

    def build(raw: Mapping[str, Any]) -> dict[str, Any]:
        sensors = (
            prog.resolve_sensors(sensor_tokens, raw.get("remoteSensors", []))
            if sensor_tokens is not None
            else None
        )
        return prog.with_comfort_setting(
            raw["program"],
            call.data[ATTR_CLIMATE_REF],
            heat_tenths=heat_tenths,
            cool_tenths=cool_tenths,
            sensors=sensors,
        )

    await _write(hass, thermostat, build)


async def _set_schedule(call: ServiceCall) -> None:
    hass = call.hass
    spec: dict[str, str] = call.data[ATTR_SCHEDULE]
    thermostats = [_thermostat_for(hass, eid) for eid in call.data[ATTR_ENTITY_ID]]

    def build(raw: Mapping[str, Any]) -> dict[str, Any]:
        return prog.with_schedule(raw["program"], spec)

    await _write_all(hass, [(thermostat, build) for thermostat in thermostats])


async def _copy_program(call: ServiceCall) -> None:
    hass = call.hass
    source = _thermostat_for(hass, call.data[ATTR_ENTITY_ID])
    targets = [
        _thermostat_for(hass, eid)
        for eid in call.data[ATTR_TARGETS]
        if eid != call.data[ATTR_ENTITY_ID]
    ]
    if not targets:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_program",
            translation_placeholders={"error": "targets must include a thermostat other than the source"},
        )
    comfort_settings: bool = call.data[ATTR_COMFORT_SETTINGS]
    schedule: bool = call.data[ATTR_SCHEDULE]
    preserve: bool = call.data[ATTR_PRESERVE_TARGET_TEMPERATURES]

    def builder(**options: bool) -> ProgramBuilder:
        def build(raw: Mapping[str, Any]) -> dict[str, Any]:
            # The source program is re-read on every build so phase 2 below (and each
            # target) sees the current source, not a snapshot.
            return prog.copy_program(
                _current_program(source),
                raw["program"],
                preserve_target_temperatures=preserve,
                **options,
            )

        return build

    # Climates the server has not created on a target yet cannot be referenced by the
    # copied schedule in the same POST (their climateRef does not exist until the write
    # returns). For such targets copy in two phases: climates only, refresh (done by
    # async_set_program), then the schedule remapped by name to the new refs.
    source_program = _current_program(source)
    two_phase = [
        target
        for target in targets
        if comfort_settings
        and schedule
        and prog.unmatched_climates(source_program, _current_program(target))
    ]
    jobs: list[tuple[Thermostat, ProgramBuilder]] = []
    for target in targets:
        LOGGER.debug("Copying program from %s to %s", source.entity_id, target.entity_id)
        if target in two_phase:
            jobs.append((target, builder(comfort_settings=True, schedule=False)))
        else:
            jobs.append(
                (target, builder(comfort_settings=comfort_settings, schedule=schedule))
            )
    await _write_all(hass, jobs)
    for target in two_phase:
        LOGGER.debug("Copying schedule to %s (phase 2)", target.entity_id)
        await _write(hass, target, builder(comfort_settings=False, schedule=True))


async def _get_program(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    thermostat = _thermostat_for(hass, call.data[ATTR_ENTITY_ID])
    try:
        return prog.program_summary(_current_program(thermostat), _unit(hass))
    except prog.ProgramError as err:
        raise _invalid(err) from err


# ------------------------------------------------------------- registration


@callback
def async_setup_program_services(hass: HomeAssistant) -> None:
    """Register the fork's program services (called from services.py)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_COMFORT_SETTING,
        _set_comfort_setting,
        schema=SET_COMFORT_SETTING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COPY_PROGRAM, _copy_program, schema=COPY_PROGRAM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PROGRAM,
        _get_program,
        schema=GET_PROGRAM_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
