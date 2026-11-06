"""Number entities for comfort-setting temperatures (ha-ecobee fork).

For every thermostat and every default climate present on it (home, away, sleep) two
CONFIG numbers are created: ``<Thermostat> <Climate> heat`` and ``… cool``. Setting one
rewrites the COMPLETE program through ``program_writer.async_set_program``.

Values are read from ``thermostat["program"]["climates"]`` (tenths °F) and exposed in °F;
Home Assistant converts to the user's unit because the device class is temperature. The
min/max follow the thermostat's own ``settings`` heat range (heat numbers) or cool range
(cool numbers), and validation uses its ``heatCoolMinDelta``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError

from . import EcobeeData
from . import program as prog
from .const import DOMAIN
from .entity import EcobeeBaseEntity
from .program_services import validate_for
from .program_writer import async_set_program

_LOGGER = logging.getLogger(__name__)

KIND_HEAT = "heat"
KIND_COOL = "cool"
_FIELD = {KIND_HEAT: "heatTemp", KIND_COOL: "coolTemp"}


def build_program_number_entities(data: EcobeeData) -> list[NumberEntity]:
    """Return heat/cool numbers for each default climate of each thermostat."""
    entities: list[NumberEntity] = []
    for index, thermostat in enumerate(data.ecobee.thermostats):
        climates = thermostat.get("program", {}).get("climates", [])
        present = {c.get("climateRef"): c for c in climates}
        for ref in prog.DEFAULT_CLIMATE_REFS:
            if ref not in present:
                continue
            for kind in (KIND_HEAT, KIND_COOL):
                entities.append(
                    EcobeeProgramTemperature(data, index, ref, present[ref]["name"], kind)
                )
    _LOGGER.debug("Adding %d ecobee program temperature numbers", len(entities))
    return entities


class EcobeeProgramTemperature(EcobeeBaseEntity, NumberEntity):
    """Heat or cool set point of one comfort setting in the thermostat program."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(
        self,
        data: EcobeeData,
        thermostat_index: int,
        climate_ref: str,
        climate_name: str,
        kind: str,
    ) -> None:
        """Initialize a program temperature number."""
        super().__init__(data, thermostat_index)
        self.climate_ref = climate_ref
        self.kind = kind
        self._attr_unique_id = f"{self.base_unique_id}-program-{climate_ref}-{kind}"
        self._attr_translation_key = f"program_{kind}"
        self._attr_translation_placeholders = {"climate": climate_name}

    def _climate(self) -> dict[str, Any] | None:
        for climate in self.thermostat.get("program", {}).get("climates", []):
            if climate.get("climateRef") == self.climate_ref:
                return climate
        return None

    def _range(self) -> tuple[int, int]:
        heat_range, cool_range = prog.temp_ranges_for(self.thermostat)
        return heat_range if self.kind == KIND_HEAT else cool_range

    @property
    @override
    def native_min_value(self) -> float:
        """Lower bound from the thermostat's heat/cool range setting."""
        return self._range()[0] / 10

    @property
    @override
    def native_max_value(self) -> float:
        """Upper bound from the thermostat's heat/cool range setting."""
        return self._range()[1] / 10

    @property
    @override
    def available(self) -> bool:
        """Available while connected and the climate still exists."""
        return super().available and self._climate() is not None

    @property
    @override
    def native_value(self) -> float | None:
        """Return the set point in °F from the cached program."""
        climate = self._climate()
        if climate is None:
            return None
        value = climate.get(_FIELD[self.kind])
        return None if value is None else value / 10

    async def async_update(self) -> None:
        """Poll ecobee (throttled; writes refresh immediately via the writer)."""
        await self.data.update()

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Write the full program with this set point changed.

        The program is built from the thermostat dict the writer has just refreshed
        (not the cache) and under the thermostat's write lock, so two numbers set in
        one service call do not overwrite each other.
        """
        tenths = prog.temp_to_tenths(value, prog.UNIT_FAHRENHEIT)

        def build(raw: Mapping[str, Any]) -> dict[str, Any]:
            try:
                program = prog.with_comfort_setting(
                    raw["program"],
                    self.climate_ref,
                    heat_tenths=tenths if self.kind == KIND_HEAT else None,
                    cool_tenths=tenths if self.kind == KIND_COOL else None,
                )
            except prog.ProgramError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_program",
                    translation_placeholders={"error": str(err)},
                ) from err
            return validate_for(raw, program)

        build(self.thermostat)  # fail fast on the cached data before any network call
        await async_set_program(self.hass, self.data, self.thermostat_index, build)
        self.async_write_ha_state()
