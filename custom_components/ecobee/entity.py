"""Base classes shared among Ecobee entities."""

import logging
from typing import Any, override

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import EcobeeData
from .const import DOMAIN, ECOBEE_MODEL_TO_NAME, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


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
        """Attach to the HomeKit device for the same thermostat when one exists.

        ha-ecobee fork: the HomeKit Device integration records each thermostat's (and
        remote sensor's) serial number, which is the ecobee identifier. Sharing that
        device's identifiers makes Home Assistant merge both integrations into ONE
        device per thermostat, so the cloud-only entities (comfort-setting numbers,
        fan minimum, on-screen messages) sit next to the local HomeKit ones instead
        of on a twin device.
        """
        info = self._attr_device_info
        if info is None or self.hass is None:
            return info
        registry = dr.async_get(self.hass)
        for device in registry.devices.values():
            if device.serial_number == self.base_unique_id and any(
                domain != DOMAIN for domain, _ in device.identifiers
            ):
                # Claim ONLY the HomeKit device's identifiers: the registry attaches
                # these entities to that existing device (a true merge). Keeping the
                # ecobee identifier as well would match our own old device first.
                merged = dict(info)
                merged["identifiers"] = set(device.identifiers)
                return DeviceInfo(**merged)  # type: ignore[typeddict-item]
        return info

    @property
    def thermostat(self) -> dict[str, Any]:
        """Return the thermostat data for the entity."""
        return self.data.ecobee.get_thermostat(self.thermostat_index)

    @property
    @override
    def available(self) -> bool:
        """Return if device is available."""
        return self.thermostat["runtime"]["connected"]
