"""Common device identity for the fan and its sensors."""

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BllcCoordinator


class BllcEntity(CoordinatorEntity[BllcCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BllcCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        identity = coordinator.client.identity
        info = coordinator.client.info
        assert identity is not None and info is not None
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        mac = ":".join(identity.mac[index:index + 2] for index in range(0, 12, 2))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=entry.title,
            manufacturer="BLLC",
            model=f"BE series ({info.wifi_hardware})",
            sw_version=info.wifi_software,
        )
