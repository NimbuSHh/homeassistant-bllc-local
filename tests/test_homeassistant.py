"""Exercise config flows and real HA entities without network or real devices."""

from dataclasses import replace
from pathlib import Path
import shutil
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant import loader, bootstrap
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.bllc_local import config_flow, coordinator
from custom_components.bllc_local.const import DOMAIN, PRODUCT_KEY
from custom_components.bllc_local.protocol import (
    AuthenticationError, BllcError, DeviceInfo, Identity, Status,
)


class Device:
    def __init__(self):
        self.status = Status("1", 23.5, 55, 75, bytes(range(12)))
        self.error = None
        self.writes = []


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    source = Path(__file__).parents[1] / "custom_components" / DOMAIN
    shutil.copytree(source, config_dir / "custom_components" / DOMAIN)
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    devices = {"192.168.50.10": Device(), "192.168.50.11": Device()}

    class FakeClient:
        def __init__(self, host, device_id="", passcode=""):
            from custom_components.bllc_local.protocol import validate_host
            self.host = validate_host(host)
            self.closed = False
            self.device_id = device_id or f"synthetic-{host.rsplit('.', 1)[-1]}"
            self.passcode = passcode or "synthetic-passcode"
            self.identity = Identity(self.device_id, "aabbccddee" + host[-2:], PRODUCT_KEY, "04020015")
            self.info = DeviceInfo("00MX3081", "04020015", "00000001", "00000001", "00000004", PRODUCT_KEY)

        async def read_status(self):
            device = devices[self.host]
            if device.error:
                raise device.error
            return device.status

        async def set_mode(self, mode):
            device = devices[self.host]
            if device.error:
                raise device.error
            device.writes.append(mode)
            device.status = replace(device.status, mode=mode)
            return device.status

        async def async_close(self):
            self.closed = True

    monkeypatch.setattr(config_flow, "BllcClient", FakeClient)
    monkeypatch.setattr(coordinator, "BllcClient", FakeClient)
    yield hass, devices
    await hass.async_stop(force=True)


async def add_device(hass, host="192.168.50.10", name="Ventilator A"):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert flow["type"] == "form"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"host": host, "name": name})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.mark.asyncio
async def test_load_controls_sensors_and_unload(environment):
    hass, devices = environment
    entry = await add_device(hass)
    fans = hass.states.async_all("fan")
    assert len(fans) == 1
    entity_id = fans[0].entity_id
    assert fans[0].state == "on"
    assert fans[0].attributes["percentage"] == 100
    assert sorted(s.state for s in hass.states.async_all("sensor")) == ["23.5", "55"]
    assert fans[0].attributes["percentage_step"] == 50
    assert fans[0].attributes["preset_modes"] == ["intermittent"]
    for percentage, mode in [(1, "2"), (33, "2"), (50, "2"), (51, "1"), (66, "1"), (100, "1"), (0, "5")]:
        await hass.services.async_call("fan", "set_percentage", {"entity_id": entity_id, "percentage": percentage}, blocking=True)
        assert devices["192.168.50.10"].status.mode == mode
        assert hass.states.get(entity_id).attributes["preset_mode"] is None
    await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert devices["192.168.50.10"].status.mode == "1"
    client = entry.runtime_data.client
    await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(entity_id).state == "unavailable"
    assert client.closed


@pytest.mark.asyncio
async def test_intermittent_is_a_preset_not_a_speed(environment):
    hass, devices = environment
    entry = await add_device(hass)
    entity_id = hass.states.async_all("fan")[0].entity_id
    device = devices["192.168.50.10"]
    await hass.services.async_call("fan", "set_preset_mode", {"entity_id": entity_id, "preset_mode": "intermittent"}, blocking=True)
    state = hass.states.get(entity_id)
    assert device.status.mode == "3"
    assert state.state == "on"
    assert state.attributes["preset_mode"] == "intermittent"
    assert state.attributes["percentage"] is None
    await hass.services.async_call("fan", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).attributes["preset_mode"] is None
    await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert device.status.mode == "3"  # Restore the actual previous operating mode.
    await hass.services.async_call("fan", "set_percentage", {"entity_id": entity_id, "percentage": 50}, blocking=True)
    assert device.status.mode == "2"
    assert hass.states.get(entity_id).attributes["preset_mode"] is None
    await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id, "preset_mode": "intermittent"}, blocking=True)
    assert device.status.mode == "3"
    writes = device.writes.copy()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("fan", "set_preset_mode", {"entity_id": entity_id, "preset_mode": "medium"}, blocking=True)
    assert device.writes == writes
    # A wall-panel change must also leave intermittent mode in HA.
    device.status = replace(device.status, mode="1")
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).attributes["preset_mode"] is None
    assert hass.states.get(entity_id).attributes["percentage"] == 100


@pytest.mark.parametrize("attribute", ["last_on_mode", "last_manual_mode"])
@pytest.mark.parametrize("mode", ["1", "2", "3"])
@pytest.mark.asyncio
async def test_restore_operating_mode_including_v010_state(environment, monkeypatch, attribute, mode):
    hass, devices = environment
    device = devices["192.168.50.10"]
    device.status = replace(device.status, mode="5")
    monkeypatch.setattr(RestoreEntity, "async_get_last_state", AsyncMock(return_value=State("fan.ventilator_a", "off", {attribute: mode})))
    await add_device(hass)
    entity_id = hass.states.async_all("fan")[0].entity_id
    assert device.writes == []  # Loading and migration must not change the device.
    await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert device.status.mode == mode


@pytest.mark.asyncio
async def test_unknown_mode_is_not_assigned_a_speed_or_preset(environment):
    hass, devices = environment
    device = devices["192.168.50.10"]
    device.status = replace(device.status, mode="4")
    await add_device(hass)
    state = hass.states.async_all("fan")[0]
    assert state.attributes["device_mode"] == "4"
    assert state.attributes["percentage"] is None
    assert state.attributes["preset_mode"] is None
    assert device.writes == []


@pytest.mark.asyncio
async def test_two_devices_and_duplicate_prevention(environment):
    hass, _ = environment
    first = await add_device(hass)
    second = await add_device(hass, "192.168.50.11", "Ventilator B")
    assert first.unique_id != second.unique_id
    assert len(hass.states.async_all("fan")) == 2
    assert len(hass.states.async_all("sensor")) == 4
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"}, data={"host": "192.168.50.10"})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_flow_errors_and_failed_control(environment):
    hass, devices = environment
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"host": "8.8.8.8"})
    assert result["errors"] == {"host": "invalid_host"}
    devices["192.168.50.10"].error = AuthenticationError("Synthetic denial")
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"host": "192.168.50.10"})
    assert result["errors"] == {"base": "invalid_auth"}
    devices["192.168.50.10"].error = None
    hass.config_entries.flow.async_abort(flow["flow_id"])
    entry = await add_device(hass)
    entity_id = hass.states.async_all("fan")[0].entity_id
    devices["192.168.50.10"].error = BllcError("Synthetic timeout")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("fan", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert devices["192.168.50.10"].status.mode == "1"
    assert hass.states.get(entity_id).state == "unavailable"
    devices["192.168.50.10"].error = None
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == "on"
