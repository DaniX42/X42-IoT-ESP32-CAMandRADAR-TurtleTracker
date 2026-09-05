# ESP32-CAM firmware

The firmware targets the AI Thinker ESP32-CAM and captures a VGA JPEG every five seconds. Each frame is posted to the backend at `/api/frames/{camera_id}`.

## HLK-LD2450 radar (optional)

An HLK-LD2450 mmWave radar can be wired next to the outdoor camera to complement motion detection with a direct, lighting-independent position and speed reading. The firmware reads it on UART2:

- LD2450 **TX** -> ESP32-CAM **GPIO13** (UART2 RX)
- LD2450 **RX** -> ESP32-CAM **GPIO14** (UART2 TX, only needed if you send configuration commands to the sensor)
- LD2450 **VIN** -> **5V**, LD2450 **GND** -> **GND**

GPIO13 and GPIO14 are free when no microSD card is in use. GPIO16 and GPIO17 must not be used because the board assigns them to PSRAM. The sensor's default baud rate is 256000, 8N1, matching the firmware's `radarSerial.begin(...)` call. Every second, the firmware parses the LD2450's binary target frames and posts the first tracked target to `/api/radar/{camera_id}` as JSON; see `docs/api.md`. If no sensor is wired, the RX pin stays idle and no radar frames are ever recognized, so the rest of the firmware is unaffected.

## Features

- Wi-Fi station mode
- HTTP JPEG frame upload
- ArduinoOTA updates after the first USB flash
- MQTT telemetry and retained Home Assistant MQTT discovery
- `/health` endpoint on the camera

## Configuration

Copy the values from `firmware/secrets.ini.example` into the local `firmware/secrets.ini`. This file is ignored by Git. Set `api_url` to the reachable backend URL, not `localhost` unless the backend runs on the camera itself.

For the current Mac-hosted backend, use the Mac's LAN address, for example `http://172.20.6.97:8000`. The backend must listen on `0.0.0.0:8000`, and the Mac firewall must allow inbound TCP 8000 from the camera network. The camera also needs outbound TCP access to the MQTT broker on port 1888.

The MQTT broker must be reachable from the camera. Home Assistant discovers these entities automatically:

- Tortoise X and Y in metres
- Tortoise last seen timestamp
- Detection confidence
- Inside-house binary sensor

## First flash

Install PlatformIO, connect the USB-to-serial adapter with GPIO0 held low during reset, then run:

```bash
cd firmware
pio run -t upload --upload-port /dev/cu.usbserial-110
pio device monitor
```

After the device joins Wi-Fi, note its IP address. Subsequent builds can use OTA:

```bash
pio run -t upload --upload-port turtle-cam-outdoor.local
```

Keep the OTA password local and set a strong value in `secrets.ini`.

## Two-camera deployment notes

The project currently uses two AI Thinker ESP32-CAM devices:

- `turtle-cam-outdoor`: the enclosure camera. It uses the default firmware profile and captures VGA JPEG frames.
- `turtle-cam-door`: the door/house camera. It uses the same backend contract but enables `TT_DOOR_CAMERA`, which changes the sensor to SVGA and applies the door-camera tuning (higher JPEG quality, brightness `2`, contrast `1`, saturation `0`, and automatic gain, exposure, and white balance).

Both cameras upload directly to the same backend at `POST /api/frames/{camera_id}`. The device name is the `camera_id`, MQTT topic suffix, OTA hostname, and the identifier used by the latest-frame endpoint. Keep the names distinct so the backend and Home Assistant can distinguish the streams:

```text
http://<backend-host>:8000/api/frames/turtle-cam-outdoor/latest
http://<backend-host>:8000/api/frames/turtle-cam-door/latest
```

The checked-in PlatformIO configuration has one `ai_thinker` environment. To build the door variant, add `-DTT_DOOR_CAMERA` to the local build flags and set `TT_DEVICE_NAME` to `turtle-cam-door`; do not commit local credentials from `secrets.ini` or generated secrets. The outdoor build must omit `TT_DOOR_CAMERA` and use `turtle-cam-outdoor`.

The backend must listen on `0.0.0.0:8000`, and the camera configuration must point to a reachable LAN address rather than `localhost`. The current development setup uses mDNS names for OTA where available (`turtle-cam-outdoor.local` and `turtle-cam-door.local`); use fixed IP addresses if multicast discovery is unreliable. After the initial USB flash, OTA uses UDP port `3232`; frame uploads use TCP port `8000`.

When diagnosing a camera, check its `/health` endpoint first, then query its latest-frame endpoint. A `404` from the latest-frame endpoint means that no valid frame from that camera has reached the backend since startup, not necessarily that the camera is offline.

## Network migration to Proxmox

When the backend moves into its own Proxmox container, change only `api_url` to the container's fixed LAN address, for example `http://192.168.1.50:8000`, then deploy the new firmware once over OTA. The container needs inbound TCP 8000 from the camera VLAN and outbound access to the MQTT broker. No USB access is required after the initial flash.

OTA itself uses UDP 3232 from the development computer to the ESP32. mDNS discovery via `*.local` uses UDP 5353 and may be replaced with the camera's fixed IP if multicast is unavailable.
