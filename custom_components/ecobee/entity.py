"""Base classes shared among Ecobee entities."""

import logging
from typing import Any, override

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import EcobeeData
from .const import DOMAIN, ECOBEE_MODEL_TO_NAME, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


def merge_into_homekit(hass, info: DeviceInfo | None) -> DeviceInfo | None:
    """ha-ecobee fork: identity pass-through.

    Merging into the HomeKit Device twin by sharing its identifiers was tried and
    reverted: since Home Assistant 2026 the device registry scopes identifier lookups
    to the calling config entry, so another integration's identifiers do not attach
    entities to its device, they create a second device with the same identifiers.
    Devices therefore stay one-per-integration; see README "HomeKit alongside".
    """
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
    def thermostat(self) -> dict[str, Any]:
        """Return the thermostat data for the entity."""
        return self.data.ecobee.get_thermostat(self.thermostat_index)

    @property
    @override
    def available(self) -> bool:
        """Return if device is available."""
        return self.thermostat["runtime"]["connected"]
