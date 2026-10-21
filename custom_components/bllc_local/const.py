"""Constants for the locally verified BLLC BE ventilator."""

DOMAIN = "bllc_local"
CONF_PASSCODE = "passcode"
CONF_DEVICE_ID = "device_id"
CONF_MAC = "mac"
PRODUCT_KEY = "a2c216633edd4c6d9d2934aeb441508c"
UDP_PORT = 12414
TCP_PORT = 12416
UPDATE_INTERVAL_SECONDS = 15
MODE_OFF = "5"
MANUAL_MODES = ("3", "2", "1")  # Low, medium, high, in speed order.
DEFAULT_ON_MODE = "2"
