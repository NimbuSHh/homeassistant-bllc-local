"""Two manual speeds and intermittent operation, confirmed by local readback."""

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

from .const import (
    DEFAULT_ON_MODE,
    MANUAL_MODES,
    MODE_INTERMITTENT,
    MODE_OFF,
    OPERATING_MODES,
    PRESET_INTERMITTENT,
)
from .coordinator import BllcCoordinator
from .entity import BllcEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BllcFan(entry.runtime_data)])


class BllcFan(BllcEntity, FanEntity, RestoreEntity):
    _attr_name = None
    _attr_translation_key = "ventilator"
    _attr_speed_count = 2
    _attr_preset_modes = [PRESET_INTERMITTENT]
    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
    )

    def __init__(self, coordinator: BllcCoordinator) -> None:
        super().__init__(coordinator, "fan")
        mode = coordinator.data.mode
        self._last_on_mode = mode if mode in OPERATING_MODES else DEFAULT_ON_MODE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if self.coordinator.data.mode not in OPERATING_MODES and previous is not None:
            # v0.1.0 stored the same raw modes under this misleading old name.
            remembered = previous.attributes.get(
                "last_on_mode", previous.attributes.get("last_manual_mode")
            )
            if remembered in OPERATING_MODES:
                self._last_on_mode = remembered

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data.mode in OPERATING_MODES:
            self._last_on_mode = self.coordinator.data.mode
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
            # Intermittent operation has no fixed speed. Mode 4 is unknown.
            return None
        return ordered_list_item_to_percentage(list(MANUAL_MODES), mode)

    @property
    def preset_mode(self) -> str | None:
        if self.coordinator.data.mode == MODE_INTERMITTENT:
            return PRESET_INTERMITTENT
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_mode": self.coordinator.data.mode,
            "last_on_mode": self._last_on_mode,
        }

    async def async_set_percentage(self, percentage: int) -> None:
        mode = MODE_OFF if percentage == 0 else percentage_to_ordered_list_item(
            list(MANUAL_MODES), percentage
        )
        await self.coordinator.async_set_mode(mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._valid_preset_mode_or_raise(preset_mode)
        await self.coordinator.async_set_mode(MODE_INTERMITTENT)

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
            return
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        current = self.coordinator.data.mode
        if current != MODE_OFF:
            return
        await self.coordinator.async_set_mode(self._last_on_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_mode(MODE_OFF)
