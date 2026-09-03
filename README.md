# Turtle Tracker

A camera-based position tracking service for one untagged tortoise in a 7 m x 2.5 m outdoor enclosure. The system combines an ESP32-CAM, an optional HLK-LD2450 mmWave radar, a Python computer-vision backend, SQLite storage, and MQTT/Home Assistant integration.

## Example images

| `turtle-cam-outdoor` (enclosure) | `turtle-cam-door` (house entrance) |
| --- | --- |
| ![Outdoor enclosure camera with calibration polygon overlay](docs/README/turtle-cam-outdoor.png) | ![Door camera with door-zone calibration lines](docs/README/turtle-cam-door.png) |

The dashed turquoise line on the outdoor image is the `ENCLOSURE_POLYGON_SOURCE` calibration overlay (`vision.draw_enclosure_overlay`); it is only drawn on `/api/frames/{camera_id}/latest` for non-door cameras. The dashed lines on the door image are the door-zone calibration lines (`vision.draw_door_calibration_overlay`), served separately at `/api/frames/turtle-cam-door/calibration` so the live door feed itself stays overlay-free.

## How it works

```text
ESP32-CAM -> HTTP JPEG upload -> FastAPI -> OpenCV motion detection
									  -> pixel-to-metre calibration
									  -> position and speed calculation
									  -> SQLite history
									  -> HTTP response -> MQTT/Home Assistant
```

1. The ESP32-CAM initializes its AI Thinker camera, connects to Wi-Fi, and captures one JPEG every five seconds.
2. The firmware sends the JPEG as an `image/jpeg` request to `/api/frames/{camera_id}`.
3. FastAPI decodes the payload with OpenCV. The current detector uses the MOG2 background-subtraction algorithm, morphological cleanup, and the largest moving contour.
4. The contour centroid is treated as the tortoise position in image pixels. Very small contours and detections below `MIN_CONFIDENCE` are rejected.
5. A four-point OpenCV homography maps image coordinates to metres in the enclosure. The tracker compares the new point with the previous point to calculate speed in metres per second.
6. Accepted positions are stored in SQLite with timestamp, coordinates, speed, confidence, and the derived `inside_house` flag.
7. The backend returns the accepted position. The ESP32-CAM publishes accepted results to MQTT, where Home Assistant discovery exposes sensors for X, Y, last seen, confidence, and whether the tortoise is inside the house.

The detector is intentionally a baseline. It can later be replaced by a tortoise classifier such as YOLO without changing the image-ingestion or persistence contracts.

### Motion and position detection in detail

The same `MotionDetector` (MOG2 background subtraction) runs for every camera, but the two cameras feed two different pipelines:

- **`turtle-cam-outdoor` (enclosure position tracking)**: The detected contour centroid is converted from pixels to real-world metres via `HomographyCalibration` (`calibration.py`), then handed to `PositionTracker` (`tracking.py`), which derives speed from the previous position and timestamp. The result becomes a `Position` row (`x`, `y`, `speed`, `confidence`, `inside_house`) available at `/api/position`, `/api/history`, `/api/heatmap`, and rendered on the schematic map at `/api/position/map`.
- **`turtle-cam-door` (in/out detection)**: The same detector's pixel coordinates are instead classified with `vision.classify_door_detection` against the entrance gap's near/far lines (`DOOR_NEAR_LINE_FRACTION`/`DOOR_FAR_LINE_FRACTION`, defined as resolution-independent fractions of the frame). `tracking.DoorCrossingTracker` compares the current side (`inside`/`outside`) to the previous one; a side change is an `entered_house` or `left_house` crossing. Crossings are written to the `events` table and update the live `inside_house` state used by subsequent enclosure positions and the `@Home` marker on `/api/position/map`.

Because motion detection alone cannot distinguish a tortoise from other movement (shadows, plants, wildlife), both pipelines only accept detections above `MIN_CONFIDENCE`, and the enclosure pipeline additionally discards anything outside the calibrated enclosure polygon.

## Frameworks and libraries

### Backend

- **Python 3.11+**: Runtime for the backend service.
- **FastAPI**: REST API, request handling, validation, and automatic OpenAPI documentation.
- **Uvicorn**: ASGI server used to run FastAPI.
- **Pydantic Settings**: Typed configuration loaded from `.env` and environment variables.
- **OpenCV**: JPEG decoding, MOG2 motion detection, contour processing, and perspective transformation.
- **NumPy**: Numeric image and coordinate operations used by OpenCV.
- **SQLite**: Embedded database for position history; no separate database server is required.
- **Pytest and HTTPX**: Unit and API testing.

### Firmware and deployment

- **Arduino framework on Espressif ESP32**: Firmware runtime for the AI Thinker ESP32-CAM.
- **PlatformIO**: Firmware project management, dependency installation, and builds.
- **ArduinoJson**: JSON parsing and serialization.
- **PubSubClient**: MQTT client used for state publishing.
- **ArduinoOTA**: Wireless firmware updates.
- **Docker and Docker Compose**: Reproducible backend deployment with persistent SQLite data.

## Components

### Backend processing

The application is created through `create_app()`, which allows tests to inject temporary settings and databases. Each app instance initializes a database, detector, calibration, and tracker. The default calibration maps VGA coordinates from `(0, 0)` to `(640, 360)` onto the configured enclosure dimensions of 7.0 m by 2.5 m.

The MOG2 detector is suitable as a development baseline, but it is sensitive to lighting changes, camera movement, shadows, and other moving objects. It currently tracks the largest moving contour and does not identify a tortoise. A production deployment should use a trained detector and an explicit enclosure calibration procedure.

### ESP32-CAM firmware

The firmware is built for the `esp32cam` board with the Arduino framework. It exposes a local `/health` endpoint, supports OTA updates, captures JPEG frames, and posts them directly to the backend. The firmware does not store images permanently.

MQTT topics are based on `turtle_tracker/{device_name}`. The device publishes availability and state messages and registers Home Assistant MQTT discovery entities after connecting to the broker.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/uvicorn turtle_tracker.app:app --reload
```

The API is available at `http://localhost:8000`. Interactive OpenAPI documentation is available at `/docs`.

For an ESP32-CAM on the same LAN, start the backend with the default `0.0.0.0` binding and set the camera's `api_url` to the host computer's LAN address. Do not use `localhost` in the firmware configuration.

Run the deterministic development frame through the backend:

```bash
curl -X POST http://localhost:8000/api/mock/frame
curl http://localhost:8000/api/position
```

## Configuration

Copy the example environment file before starting locally or with Docker:

```bash
cp .env.example .env
```

The backend settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `data/turtle_tracker.db` | SQLite database location |
| `MOCK_IMAGES_ENABLED` | `true` | Enables the deterministic mock endpoint |
| `ENCLOSURE_LENGTH_METERS` | `7.0` | Real-world enclosure length |
| `ENCLOSURE_WIDTH_METERS` | `2.5` | Real-world enclosure width |
| `HOUSE_X_METERS` | `7.0` | House position on the enclosure map, used while indoor |
| `HOUSE_Y_METERS` | `2.5` | House position on the enclosure map, used while indoor |
| `MIN_CONFIDENCE` | `0.02` | Minimum accepted motion confidence |

Never commit `.env`, `firmware/secrets.ini`, or `firmware/src/generated_secrets.h`. The repository contains example files only. Firmware credentials are generated locally from `firmware/secrets.ini` by the PlatformIO pre-build script.

## Firmware setup

1. Copy `firmware/secrets.ini.example` to `firmware/secrets.ini`.
2. Set the Wi-Fi, backend URL, MQTT, device name, and OTA values in the local secrets file.
3. Ensure the backend URL uses the host computer's LAN IP address, for example `http://192.168.1.20:8000`.
4. Build and upload from the `firmware` directory with PlatformIO:

```bash
pio run
pio run --target upload
pio device monitor
```

The camera uses VGA JPEG frames and sends them every five seconds. The backend must be reachable from the camera, and port `8000` must be allowed through the host firewall.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Service health |
| GET | `/api/position` | Latest tracked position |
| GET | `/api/history?limit=1000` | Recent positions |
| GET | `/api/heatmap?grid=0.5` | Binned position counts |
| POST | `/api/frames/{camera_id}` | Ingest a JPEG as multipart field `file` |
| POST | `/api/mock/frame` | Ingest a generated development frame |

The frame endpoint accepts either a raw `image/jpeg` request body, as sent by the firmware, or a multipart upload using the field name `file`. A successful frame response contains `accepted: true` and the calculated position. Frames without confident motion are acknowledged with `accepted: false` and are not stored.

Position fields are expressed in metres except for `speed` (metres per second), `confidence` (0 to 1), and the ISO 8601 `timestamp`.

## Home Assistant image

The latest received JPEG is available at:

```text
http://<backend-host>:8000/api/frames/<camera-id>/latest
```

For the default firmware configuration, `<camera-id>` is the value of `TT_DEVICE_NAME`. Add a Generic Camera to Home Assistant, replacing the host and device name with the values used in your setup:

```yaml
# configuration.yaml
camera:
	- platform: generic
		name: Turtle Tracker Camera
		still_image_url: http://192.168.1.20:8000/api/frames/terrarium_turtleCamOutdoor/latest
		verify_ssl: false
```

Restart or reload the Home Assistant configuration, then add the `camera.turtle_tracker_camera` entity to a dashboard with a Picture card. Home Assistant and the backend must be able to reach each other over the network. The latest frame is kept in backend memory, so it becomes available again when the next camera frame arrives after a backend restart.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

SQLite data is persisted in `./data`.

The backend can later be moved to a dedicated Proxmox container without changing the API or MQTT contract; update the camera `api_url` to the container's fixed address.

## Development and tests

Run the test suite with:

```bash
.venv/bin/python -m pytest
```

The test configuration adds `src` to the Python path and discovers tests in `tests/`. API tests can inject a temporary SQLite database through `create_app()`, so they do not require Docker or a running camera.

## Repository layout

```text
src/turtle_tracker/  Application package
tests/                Unit and API tests
docs/                 Architecture and calibration notes
firmware/             PlatformIO ESP32-CAM firmware
data/                 Local SQLite persistence volume
Dockerfile            Production container
docker-compose.yml    Local deployment
```
