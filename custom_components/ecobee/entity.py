"""Base classes shared among Ecobee entities."""

import logging
from typing import Any, override

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import EcobeeData
from .const import DOMAIN, ECOBEE_MODEL_TO_NAME, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


def merge_into_homekit(hass, info: DeviceInfo | None) -> DeviceInfo | None:
    """ha-ecobee fork: return ``info`` re-targeted at the HomeKit device for the same thermostat/sensor.

    HomeKit Device stores the ecobee identifier as the device serial number; claiming that
    device's identifiers makes the registry attach our entities to it (one device per
    thermostat). Without a HomeKit twin the info is returned unchanged.
    """
    if not info or hass is None:
        return info
    serials = {value for domain, value in info.get("identifiers", ()) if domain == DOMAIN}
    if not serials:
        return info
    registry = dr.async_get(hass)
    for device in registry.devices.values():
        if device.serial_number in serials and any(d != DOMAIN for d, _ in device.identifiers):
            merged = dict(info)
            merged["identifiers"] = set(device.identifiers)
            return DeviceInfo(**merged)  # type: ignore[typeddict-item]
    return info


class EcobeeBaseEntity(Entity):
    """Base methods for Ecobee entities."""

    _attr_has_entity_name = True

    def __init__(self, data: EcobeeData, thermostat_index: int) -> None:
        """Initiate base methods for Ecobee entities."""
        self.data = data
        self.thermostat_index = thermostat_index
        thermostat = self.thermostat
        self.base_unique_id = thermostat["identifier"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, thermostat["identifier"])},
            manufacturer=MANUFACTURER,
            model=ECOBEE_MODEL_TO_NAME.get(thermostat["modelNumber"]),
            name=thermostat["name"],
        )

    @property
    @override
    def device_info(self) -> DeviceInfo | None:
        """Device info, merged into the HomeKit twin when one exists (ha-ecobee fork)."""
        return merge_into_homekit(self.hass, self._attr_device_info)

    @property
    def thermostat(self) -> dict[str, Any]:
        """Return the thermostat data for the entity."""
        return self.data.ecobee.get_thermostat(self.thermostat_index)

    @property
    @override
    def available(self) -> bool:
        """Return if device is available."""
        return self.thermostat["runtime"]["connected"]
