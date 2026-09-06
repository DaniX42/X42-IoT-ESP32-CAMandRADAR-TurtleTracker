# REST API

The service exposes OpenAPI documentation at `/docs`.

## Current position

`GET /api/position` returns the latest position or `404` if no frame has produced a detection.

```json
{
  "timestamp": "2026-08-22T10:00:00Z",
  "x": 2.3,
  "y": 1.7,
  "inside_house": false,
  "speed": 0.04,
  "confidence": 0.72
}
```

## History

`GET /api/history?limit=1000` returns recent positions, newest first.

## Heatmap data

`GET /api/heatmap?grid=0.5` returns counts grouped into square metre bins.

## Position map

`GET /api/position/map` returns a schematic top-down JPEG of the enclosure with the tortoise's last known position marked. While `inside_house` is currently true (from the door camera, live state), it shows an `@Home` marker instead of coordinates.

## Frame ingestion

`POST /api/frames/{camera_id}` accepts a JPEG in multipart field `file`.

`POST /api/mock/frame` runs the same path using a generated JPEG while camera hardware is unavailable.

## Radar ingestion

`POST /api/radar/{camera_id}` accepts an HLK-LD2450 target report and stores it as a position with `source: "radar"`:

```json
{
  "targets": [
    {"x_mm": 350, "y_mm": 4200, "speed_mm_s": 0}
  ]
}
```

Only the first target is used (the enclosure has a single tracked tortoise). Coordinates are in the sensor's native millimetre frame and are converted to enclosure metres via `RadarCalibration` (`calibration.py`), configurable with the `radar_offset_length_meters`, `radar_offset_width_meters`, and `radar_mirror_width` settings. Radar positions are accepted with `confidence: 1.0` and share the same `positions` table and `/api/position`, `/api/history`, `/api/heatmap` endpoints as camera-derived positions.

`GET /api/radar/{camera_id}/targets` returns the latest complete target report received from the radar. It returns `404` until the first report arrives.

`GET /api/radar/{camera_id}/view` provides a live browser radar view. It polls the targets endpoint every second, plots up to three targets in the LD2450's native coordinate frame, and displays their X/Y positions in millimetres.

## Latest frame

`GET /api/frames/{camera_id}/latest` returns the latest valid JPEG received for the camera with `Content-Type: image/jpeg`. It returns `404` until the first frame has been received. The endpoint is intended for Home Assistant's Generic Camera integration.

`turtle-cam-door` frames never carry an overlay on this endpoint.

## Motion crop review

Each saved motion crop receives a perceptual hash. A new crop that is at least 95% similar to an existing reference crop is discarded automatically. Existing crops are checked during application startup as well. Crops explicitly retained for training are never removed automatically.

`GET /api/motion-crops?limit=50&offset=0` returns a page of up to 50 saved crops, their current labels, and the total count. `GET /api/motion-crops/{filename}` returns the JPEG for review.

`GET /api/motion-crops/review?page=1` provides a browser review grid. Select any number of crops, assign them `Yes` or `No`, optionally retain negative examples for training, then save all selections in one request. Each page contains at most 50 crops.

`POST /api/motion-crops/{filename}/label` accepts a review decision:

```json
{
  "is_turtle": false,
  "keep_for_training": false
}
```

Setting `is_turtle` to `true` keeps a positive training example. A negative label deletes the crop by default. Set `keep_for_training` to `true` to retain a negative example that is useful for training.

`POST /api/motion-crops/labels` accepts multiple label objects in one request:

```json
{
  "items": [
    {"filename": "crop-a.jpg", "is_turtle": true},
    {"filename": "crop-b.jpg", "is_turtle": false, "keep_for_training": true}
  ]
}
```

## Door calibration frame

`GET /api/frames/turtle-cam-door/calibration` returns the latest door-camera JPEG with the door entrance lines (near/far edges of the entrance gap) drawn on top, for calibrating `vision.DOOR_NEAR_LINE_FRACTION` / `DOOR_FAR_LINE_FRACTION`. It is calibration-only and never served on `/latest`.

## Door crossing detection

Motion detected on the `turtle-cam-door` camera is classified as `inside` or `outside` relative to the midpoint between the two entrance lines. A side change writes an `entered_house` or `left_house` row to the `events` table and updates `inside_house` on the next enclosure position.

## MQTT

When `mqtt_enabled` is set (see `Settings` in `config.py`), house state is also published to MQTT:

- `turtle_tracker/house/state` (retained): `inside` or `outside`.
- `turtle_tracker/house/event`: `entered_house` or `left_house`, published once per crossing.

The topic prefix is configurable via `mqtt_topic_prefix` (default `turtle_tracker/house`).
