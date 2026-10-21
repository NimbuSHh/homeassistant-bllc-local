"""Power and three manual speeds, confirmed by local status readback."""

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .const import DEFAULT_ON_MODE, MANUAL_MODES, MODE_OFF
from .coordinator import BllcCoordinator
from .entity import BllcEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BllcFan(entry.runtime_data)])


class BllcFan(BllcEntity, FanEntity, RestoreEntity):
    _attr_name = None
    _attr_speed_count = 3
    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
    )

    def __init__(self, coordinator: BllcCoordinator) -> None:
        super().__init__(coordinator, "fan")
        mode = coordinator.data.mode
        self._last_manual_mode = mode if mode in MANUAL_MODES else DEFAULT_ON_MODE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if self.coordinator.data.mode not in MANUAL_MODES and previous is not None:
            remembered = previous.attributes.get("last_manual_mode")
            if remembered in MANUAL_MODES:
                self._last_manual_mode = remembered

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data.mode in MANUAL_MODES:
            self._last_manual_mode = self.coordinator.data.mode
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.mode != MODE_OFF

    @property
    def percentage(self) -> int | None:
        mode = self.coordinator.data.mode
        if mode == MODE_OFF:
            return 0
        if mode not in MANUAL_MODES:
            # The meaning of mode 4 is not established for this installation.
            return None
        return ordered_list_item_to_percentage(list(MANUAL_MODES), mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_mode": self.coordinator.data.mode,
            "last_manual_mode": self._last_manual_mode,
        }

    async def async_set_percentage(self, percentage: int) -> None:
        mode = MODE_OFF if percentage == 0 else percentage_to_ordered_list_item(
            list(MANUAL_MODES), percentage
        )
        await self.coordinator.async_set_mode(mode)

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        current = self.coordinator.data.mode
        if current != MODE_OFF:
            return
        await self.coordinator.async_set_mode(self._last_manual_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_mode(MODE_OFF)
