"""Constants for the HDHR Local integration."""
DOMAIN = "hdhr"
VERSION = "0.9.0"

CONF_HOSTS = "hosts"                 # optional static device addresses (comma separated in the UI)
CONF_DEVICE_NAMES = "device_names"   # {device id: name}
CONF_CLIENT_NAMES = "client_names"   # {client IP: name} - "who is watching"
CONF_PANEL_TITLE = "panel_title"
CONF_PLAYER_REMOTES = "player_remotes"   # "Player name=remote.entity, ..." - how hdhr.tune reaches each TV
CONF_TV_APP = "tv_app"                   # app hdhr.tune opens when nothing is playing (package name)
DEFAULT_TV_APP = "com.silicondust.view"

DEFAULT_PANEL_TITLE = "HDHomeRun"
PANEL_URL = "hdhr"
STATIC_URL = "/hdhr_static"

POLL_SECONDS = 30
SCAN_EVERY_POLLS = 20                # rediscover devices + refresh recordings every 10 min
