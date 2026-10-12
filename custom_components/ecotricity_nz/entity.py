"""Base entities for Ecotricity NZ."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


class EcotricityEntity[CoordinatorT: DataUpdateCoordinator[Any]](CoordinatorEntity[CoordinatorT]):
    """An entity attached to the ICP's service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CoordinatorT, icp: str, description: EntityDescription) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{icp}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, icp)},
            name=f"ICP {icp}",
            manufacturer="Ecotricity",
            model="Electricity connection",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://portal.ecotricity.co.nz",
        )
