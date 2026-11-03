# HDHR Local

An **unofficial** Home Assistant integration for HDHomeRun network tuners and the HDHomeRun DVR (RECORD engine).
It works locally against your own devices, and uses SiliconDust's documented cloud API only for the guide and
recording schedule when you have the HDHomeRun DVR service.

> **Not affiliated with, endorsed by or supported by SiliconDust USA Inc.** HDHomeRun is a trademark of SiliconDust
> USA Inc., used here only to say which devices this works with.

## What it gives you

- **Tuners:** channel, who is watching, signal strength / quality / symbol quality, stream errors and running totals,
  bitrate, and the show on each tuner right now.
- **DVR:** free space, recordings, the upcoming schedule (sensor + calendar), and a warning when more recordings
  overlap than you have tuners.
- **Players:** which channel each TV is on and what is playing, named the way you name them.
- **A built-in dashboard** (sidebar panel) with an Overview, a searchable two-week **Guide** (DVR service required)
  and signal-history graphs.
- **Actions for voice assistants and automations:**
  - `hdhr.record` - record a show (every new airing or just the next one), by title.
  - `hdhr.tune` - put a TV on a channel, or on a show that is on now, through that TV's Home Assistant remote.
    **Experimental:** tested on an NVIDIA Shield running the HDHomeRun app with the Android TV Remote integration.
    It confirms where the TV landed and reports a failure rather than guessing.
  - `hdhr.signal_test` - follow an appliance (any on/off entity) and compare reception while it runs vs. after.

## Requirements

- Home Assistant 2026.10 or newer.
- One or more HDHomeRun tuners on your network. The DVR features need an HDHomeRun RECORD engine and, for the guide
  and schedule, the HDHomeRun DVR service.

## Install

**HACS:** add this repository as a custom repository (category: Integration), install **HDHR Local**, restart Home
Assistant, then add it under Settings > Devices & services. Devices are found automatically; you can also enter
addresses by hand.

**Manual:** copy `custom_components/hdhr` into your Home Assistant `config/custom_components/` folder and restart.

Names for devices and TVs, the panel title and the remote used for each TV are all set in the integration's
Configure screen.

## Privacy and the cloud

Device status is read locally from your tuners and DVR. For the guide and recording schedule the integration reads
your tuners' DeviceAuth codes at the moment of each request and sends them only to `api.hdhomerun.com` over HTTPS,
as SiliconDust's documentation describes. They are never stored or logged. Nothing is sent anywhere else.

SiliconDust's public documentation:
[HDHomeRun HTTP API](https://info.hdhomerun.com/info/http_api),
[DVR API](https://info.hdhomerun.com/info/dvr_api),
[Guide API](https://github.com/Silicondust/documentation/wiki).

## License

[Apache License 2.0](LICENSE). Provided as-is, without warranty; support is best effort.

Created by [@nathandlawrence](https://github.com/nathandlawrence).
