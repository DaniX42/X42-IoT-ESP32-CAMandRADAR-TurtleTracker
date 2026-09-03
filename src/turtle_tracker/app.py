from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse

from .calibration import HomographyCalibration, RadarCalibration
from .config import Settings, get_settings
from .db import Database, row_to_dict
from .models import (
    HeatmapPoint,
    IngestResponse,
    MotionCrop,
    MotionCropLabel,
    MotionCropLabelBatch,
    MotionCropPage,
    Position,
    RadarFrame,
)
from .mock import mock_jpeg
from .mqtt import MqttPublisher
from .tracking import DoorCrossingTracker, PositionTracker, RadarPositionTracker
from .vision import (
    Detection,
    MotionDetector,
    classify_door_detection,
    crop_to_enclosure,
    decode_jpeg,
    draw_detection_overlay,
    draw_door_calibration_overlay,
    draw_enclosure_overlay,
    draw_house_overlay,
    draw_position_map,
    in_enclosure_polygon,
)

DOOR_CAMERA_ID = "turtle-cam-door"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _perceptual_hash(image: np.ndarray) -> str:
    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(grayscale, (32, 32), interpolation=cv2.INTER_AREA)
    coefficients = cv2.dct(np.float32(resized))[:8, :8]
    median = np.median(coefficients[1:, :])
    bits = (coefficients > median).flatten()
    return f"{sum(int(bit) << index for index, bit in enumerate(bits)):016x}"


def _is_near_duplicate(perceptual_hash: str, known_hashes: list[str]) -> bool:
    value = int(perceptual_hash, 16)
    return any((value ^ int(known_hash, 16)).bit_count() <= 3 for known_hash in known_hashes)


def _save_motion_crop(
    image: np.ndarray,
    detection: Detection,
    timestamp: datetime,
    camera_id: str,
    crops_path: Path,
    database: Database,
) -> None:
    """Save a crop of detected motion for later training data labeling."""
    crop = image[detection.y_min : detection.y_max, detection.x_min : detection.x_max]
    if crop.size == 0:
        return
    perceptual_hash = _perceptual_hash(crop)
    if _is_near_duplicate(perceptual_hash, database.motion_crop_hashes()):
        return
    crops_path.mkdir(parents=True, exist_ok=True)
    filename = f"{timestamp.isoformat()}_{camera_id}.jpg"
    success, encoded = cv2.imencode(".jpg", crop)
    if success:
        (crops_path / filename).write_bytes(encoded.tobytes())
        database.insert_motion_crop(filename, camera_id, timestamp.isoformat(), perceptual_hash)


def _register_existing_motion_crops(crops_path: Path, database: Database) -> None:
    if not crops_path.exists():
        return
    known_hashes: list[str] = []
    for path in sorted(crops_path.glob("*.jpg")):
        crop = database.motion_crop(path.name)
        image = cv2.imread(str(path))
        if image is None:
            continue
        perceptual_hash = _perceptual_hash(image)
        if crop is not None and crop["keep_for_training"]:
            known_hashes.append(perceptual_hash)
            continue
        if _is_near_duplicate(perceptual_hash, known_hashes):
            path.unlink(missing_ok=True)
            if crop is not None:
                database.delete_motion_crop(path.name)
            continue
        known_hashes.append(perceptual_hash)
        if crop is None:
            camera_id = path.stem.rsplit("_", 1)[-1]
            captured_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            database.insert_motion_crop(path.name, camera_id, captured_at, perceptual_hash)


def _prepare_frame(camera_id: str, payload: bytes) -> object:
    image = decode_jpeg(payload)
    if camera_id != DOOR_CAMERA_ID:
        image = draw_enclosure_overlay(image)
        image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        image = crop_to_enclosure(image)
    else:
        image = draw_door_calibration_overlay(image)
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    return image


def _rotated_detection(detection: Detection, camera_id: str, width: int, height: int) -> Detection:
    if camera_id != DOOR_CAMERA_ID:
        x_pixel = int(detection.y_pixel)
        y_pixel = int(width - 1 - detection.x_pixel)
    else:
        x_pixel = int(height - 1 - detection.y_pixel)
        y_pixel = int(detection.x_pixel)
    return Detection(
        x_pixel=x_pixel,
        y_pixel=y_pixel,
        confidence=detection.confidence,
        x_min=detection.x_min,
        y_min=detection.y_min,
        x_max=detection.x_max,
        y_max=detection.y_max,
    )


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_path)
    database.initialize()
    _register_existing_motion_crops(settings.motion_crops_path, database)
    calibration = HomographyCalibration(
        [[0, 0], [640, 0], [640, 360], [0, 360]],
        settings.enclosure_length_meters,
        settings.enclosure_width_meters,
    )
    detector = MotionDetector()
    tracker = PositionTracker(calibration, settings.enclosure_length_meters, settings.enclosure_width_meters)
    radar_calibration = RadarCalibration(
        settings.enclosure_width_meters,
        settings.radar_offset_length_meters,
        settings.radar_offset_width_meters,
        settings.radar_mirror_width,
    )
    radar_tracker = RadarPositionTracker(settings.enclosure_length_meters, settings.enclosure_width_meters)
    door_tracker = DoorCrossingTracker()
    house_state = {"inside_house": False}
    mqtt_publisher = MqttPublisher(
        settings.mqtt_host,
        settings.mqtt_port,
        settings.mqtt_user,
        settings.mqtt_password,
        settings.mqtt_topic_prefix,
        settings.mqtt_enabled,
    )
    latest_frames: dict[str, bytes] = {}
    latest_detections: dict[str, object] = {}  # Store Detection objects for visualization
    latest_detection_times: dict[str, datetime] = {}  # Store timestamp of last detection
    detection_history: dict[str, list[tuple[Detection, datetime]]] = {}  # Store top-3 detections with timestamps

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="Turtle Tracker API", version="0.1.0", lifespan=lifespan)

    async def process_frame(camera_id: str, payload: bytes) -> IngestResponse:
        if not camera_id.strip():
            raise HTTPException(status_code=400, detail="camera_id is required")
        if not payload:
            raise HTTPException(status_code=400, detail="JPEG payload is required")
        try:
            image = decode_jpeg(payload)
            detection = detector.detect(image)
        except ValueError as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
        latest_frames[camera_id] = payload
        if detection is None or detection.confidence < settings.min_confidence:
            return IngestResponse(accepted=False, reason="No confident motion detected")
        timestamp = _utc_now()
        if camera_id == DOOR_CAMERA_ID:
            latest_detections[camera_id] = detection
            latest_detection_times[camera_id] = timestamp
            # Update detection history (keep top-3 by confidence)
            if camera_id not in detection_history:
                detection_history[camera_id] = []
            detection_history[camera_id].append((detection, timestamp))
            detection_history[camera_id].sort(key=lambda x: x[0].confidence, reverse=True)
            detection_history[camera_id] = detection_history[camera_id][:3]
            _save_motion_crop(image, detection, timestamp, camera_id, settings.motion_crops_path, database)
            side = classify_door_detection(detection.x_pixel, detection.y_pixel, image.shape[1], image.shape[0])
            crossing = door_tracker.update(side, timestamp)
            if crossing is not None:
                house_state["inside_house"] = crossing.event == "entered_house"
                database.insert_event(timestamp.isoformat(), crossing.event)
                mqtt_publisher.publish_event(crossing.event)
                mqtt_publisher.publish_state(house_state["inside_house"])
            return IngestResponse(accepted=True)
        if not in_enclosure_polygon(detection.x_pixel, detection.y_pixel, image.shape[1], image.shape[0]):
            return IngestResponse(accepted=False, reason="Motion detected outside the enclosure")
        latest_detections[camera_id] = detection
        latest_detection_times[camera_id] = timestamp
        # Update detection history (keep top-3 by confidence)
        if camera_id not in detection_history:
            detection_history[camera_id] = []
        detection_history[camera_id].append((detection, timestamp))
        detection_history[camera_id].sort(key=lambda x: x[0].confidence, reverse=True)
        detection_history[camera_id] = detection_history[camera_id][:3]
        _save_motion_crop(image, detection, timestamp, camera_id, settings.motion_crops_path, database)
        track = tracker.update(detection, timestamp)
        position = Position(
            timestamp=timestamp,
            x=track.x,
            y=track.y,
            inside_house=house_state["inside_house"],
            speed=track.speed,
            confidence=detection.confidence,
        )
        database.insert_position(timestamp.isoformat(), position.x, position.y, position.inside_house, position.speed, position.confidence)
        mqtt_publisher.publish_position(position.x, position.y, position.speed, position.confidence)
        return IngestResponse(accepted=True, position=position)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def apply_motion_crop_label(filename: str, label: MotionCropLabel) -> bool:
        crop = database.motion_crop(filename)
        if crop is None:
            raise HTTPException(status_code=404, detail="Motion crop not found")
        if not label.is_turtle and not label.keep_for_training:
            path = settings.motion_crops_path / filename
            path.unlink(missing_ok=True)
            database.delete_motion_crop(filename)
            return True
        database.label_motion_crop(filename, label.is_turtle, label.keep_for_training)
        return False

    @app.get("/api/motion-crops", response_model=MotionCropPage)
    def list_motion_crops(limit: int = 50, offset: int = 0, is_turtle: bool | None = None) -> MotionCropPage:
        limit = min(max(limit, 1), 50)
        offset = max(offset, 0)
        return MotionCropPage(
            items=[MotionCrop(**row_to_dict(row)) for row in database.motion_crops(is_turtle, limit, offset)],
            total=database.motion_crop_count(is_turtle),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/motion-crops/review", response_class=HTMLResponse)
    def motion_crop_review(page: int = 1) -> HTMLResponse:
        current_page = max(page, 1)
        return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Motion crop review</title><style>
body {{ margin: 0; background: #eef2f0; color: #17211d; font: 16px Georgia, serif; }}
header {{ position: sticky; top: 0; z-index: 1; display: flex; gap: 12px; align-items: center; padding: 14px 20px; background: #ffffff; border-bottom: 1px solid #c6d0ca; }}
button, a {{ border: 1px solid #59665e; background: #ffffff; color: #17211d; padding: 8px 12px; border-radius: 4px; font: inherit; cursor: pointer; text-decoration: none; }}
button.primary {{ background: #176b48; border-color: #176b48; color: white; }} button.no {{ background: #8e3328; border-color: #8e3328; color: white; }}
#count {{ margin-left: auto; }} main {{ padding: 18px; }}
#grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(165px, 1fr)); gap: 12px; }}
.crop {{ background: white; border: 1px solid #c6d0ca; border-radius: 6px; overflow: hidden; }}
.crop-media {{ position: relative; display: block; }}
.crop img {{ display: block; width: 100%; aspect-ratio: 1; object-fit: cover; background: #d5ddd8; cursor: pointer; }}
.crop-status {{ position: absolute; inset: 10px 10px auto auto; padding: 6px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; letter-spacing: 0.08em; border: 1px solid rgba(0,0,0,.2); backdrop-filter: blur(2px); }}
.crop-status.empty {{ background: rgba(23, 31, 29, 0.3); color: #f4f8f5; }}
.crop-status.yes {{ background: rgba(23, 107, 72, 0.88); color: white; }}
.crop-status.no {{ background: rgba(142, 51, 40, 0.88); color: white; }}
.crop label {{ display: flex; align-items: center; gap: 6px; padding: 9px; }}
footer {{ display: flex; justify-content: center; gap: 12px; padding: 24px; }}
</style></head><body><header><strong>Motion crop review</strong><button id="select-all">Select all on this page</button><button class="primary" id="yes">Selected: Yes</button><button class="no" id="no">Selected: No</button><label><input type="checkbox" id="keep"> Keep No images as training examples</label><button id="save">Save</button><span id="count"></span></header><main><div id="grid"></div></main><footer><a id="previous">Previous</a><a id="next">Next</a></footer>
<script>
const page = {current_page}, limit = 50, offset = (page - 1) * limit, choices = new Map();
const grid = document.querySelector('#grid'), selected = () => [...document.querySelectorAll('.pick:checked')].map(input => input.value);
function syncCropStatus() {{
  document.querySelectorAll('.pick').forEach(input => {{
    const filename = input.value;
    const choice = choices.get(filename);
    const article = input.closest('.crop');
    const status = article?.querySelector('.crop-status');
    const checked = !!choice;
    input.checked = checked;
    article?.classList.toggle('selected', checked);
    status && (status.className = `crop-status ${{choice ? (choice.is_turtle ? 'yes' : 'no') : 'empty'}}`);
    if (status) status.textContent = choice ? (choice.is_turtle ? 'YES' : 'NO') : 'SELECT';
  }});
}}
function toggleCropSelection(filename) {{
  const input = document.querySelector(`.pick[value="${{CSS.escape(filename)}}"]`);
  if (!input) return;
  const nextChecked = !input.checked;
  input.checked = nextChecked;
  if (!nextChecked) {{
    choices.delete(filename);
  }} else {{
    const current = choices.get(filename);
    choices.set(filename, {{filename, is_turtle: current?.is_turtle ?? true, keep_for_training: current ? current.keep_for_training : (!current?.is_turtle && document.querySelector('#keep').checked)}});
  }}
  syncCropStatus();
}}
async function load() {{
  const data = await fetch(`/api/motion-crops?limit=${{limit}}&offset=${{offset}}`).then(response => response.json());
  document.querySelector('#count').textContent = `${{data.total}} images`;
  grid.innerHTML = data.items.map(crop => {{
    const choice = choices.get(crop.filename);
    const state = choice ? (choice.is_turtle ? 'YES' : 'NO') : 'SELECT';
    const tone = choice ? (choice.is_turtle ? 'yes' : 'no') : 'empty';
    return `<article class="crop"><div class="crop-media"><img loading="lazy" src="/api/motion-crops/${{encodeURIComponent(crop.filename)}}" alt="Motion crop" data-filename="${{crop.filename}}"><div class="crop-status ${{tone}}" aria-live="polite">${{state}}</div></div><label><input class="pick" type="checkbox" value="${{crop.filename}}" ${{choice ? 'checked' : ''}} data-filename="${{crop.filename}}"> Select</label></article>`;
  }}).join('');
  document.querySelectorAll('.pick').forEach(input => {{
    input.onchange = () => {{
      const filename = input.value;
      if (input.checked) {{
        const current = choices.get(filename);
        choices.set(filename, {{filename, is_turtle: current?.is_turtle ?? true, keep_for_training: current ? current.keep_for_training : false}});
      }} else {{
        choices.delete(filename);
      }}
      syncCropStatus();
    }};
  }});
  document.querySelectorAll('.crop img').forEach(img => {{
    img.onclick = () => toggleCropSelection(img.dataset.filename);
  }});
  document.querySelector('#previous').href = page > 1 ? `/api/motion-crops/review?page=${{page - 1}}` : '#';
  document.querySelector('#next').href = offset + limit < data.total ? `/api/motion-crops/review?page=${{page + 1}}` : '#';
}}
function mark(isTurtle) {{ selected().forEach(filename => choices.set(filename, {{filename, is_turtle: isTurtle, keep_for_training: !isTurtle && document.querySelector('#keep').checked}})); syncCropStatus(); }}
document.querySelector('#select-all').onclick = () => {{ const picks = [...document.querySelectorAll('.pick')]; const select = picks.some(input => !input.checked); picks.forEach(input => input.checked = select); if (select) {{ picks.forEach(input => {{ const current = choices.get(input.value); choices.set(input.value, {{filename: input.value, is_turtle: current?.is_turtle ?? true, keep_for_training: current ? current.keep_for_training : false}}); }}); }} else {{ picks.forEach(input => choices.delete(input.value)); }} syncCropStatus(); }};
document.querySelector('#yes').onclick = () => mark(true); document.querySelector('#no').onclick = () => mark(false);
document.querySelector('#save').onclick = async () => {{ if (!choices.size) return; await fetch('/api/motion-crops/labels', {{method: 'POST', headers: {{'content-type': 'application/json'}}, body: JSON.stringify({{items: [...choices.values()]}})}}); location.reload(); }};
load();
</script></body></html>""")

    @app.post("/api/motion-crops/labels")
    def label_motion_crops(labels: MotionCropLabelBatch) -> dict[str, int]:
        deleted = sum(apply_motion_crop_label(item.filename, item) for item in labels.items)
        return {"processed": len(labels.items), "deleted": deleted}

    @app.get("/api/motion-crops/{filename}", response_class=Response)
    def get_motion_crop(filename: str) -> Response:
        crop = database.motion_crop(filename)
        path = settings.motion_crops_path / filename
        if crop is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Motion crop not found")
        return Response(content=path.read_bytes(), media_type="image/jpeg")

    @app.post("/api/motion-crops/{filename}/label")
    def label_motion_crop(filename: str, label: MotionCropLabel) -> dict[str, bool]:
        return {"deleted": apply_motion_crop_label(filename, label)}

    @app.get("/api/frames/{camera_id}/latest", response_class=Response)
    def latest_frame(camera_id: str) -> Response:
        payload = latest_frames.get(camera_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="No frame received")
        image = _prepare_frame(camera_id, payload)  # Already includes cropping for outdoor
        success, rotated_payload = cv2.imencode(".jpg", image)
        if not success:
            raise HTTPException(status_code=500, detail="Could not encode latest frame")
        return Response(content=rotated_payload.tobytes(), media_type="image/jpeg")

    @app.get("/api/frames/turtle-cam-door/calibration", response_class=Response)
    def door_calibration_frame() -> Response:
        payload = latest_frames.get(DOOR_CAMERA_ID)
        if payload is None:
            raise HTTPException(status_code=404, detail="No frame received")
        image = draw_door_calibration_overlay(decode_jpeg(payload))
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        success, encoded = cv2.imencode(".jpg", image)
        if not success:
            raise HTTPException(status_code=500, detail="Could not encode calibration frame")
        return Response(content=encoded.tobytes(), media_type="image/jpeg")

    @app.get("/api/frames/{camera_id}/latest/square", response_class=Response)
    @app.get("/api/frames/{camera_id}/latest/square.jpg", response_class=Response)
    def latest_frame_square(camera_id: str) -> Response:
        payload = latest_frames.get(camera_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="No frame received")
        image = _prepare_frame(camera_id, payload)  # Already includes cropping
        height, width = image.shape[:2]
        size = 500
        scale = min(size / width, size / height)
        image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        height, width = image.shape[:2]
        background_scale = max(size / width, size / height)
        background = cv2.resize(
            image,
            (round(width * background_scale), round(height * background_scale)),
            interpolation=cv2.INTER_LINEAR,
        )
        background = cv2.GaussianBlur(background, (0, 0), 18)
        background_height, background_width = background.shape[:2]
        background = background[
            (background_height - size) // 2 : (background_height - size) // 2 + size,
            (background_width - size) // 2 : (background_width - size) // 2 + size,
        ]
        x_offset = (size - width) // 2
        y_offset = (size - height) // 2
        background[y_offset : y_offset + height, x_offset : x_offset + width] = image
        square = background
        success, square_payload = cv2.imencode(".jpg", square)
        if not success:
            raise HTTPException(status_code=500, detail="Could not encode square latest frame")
        return Response(content=square_payload.tobytes(), media_type="image/jpeg")

    @app.get("/api/frames/{camera_id}/latest/with-detection", response_class=Response)
    def latest_frame_with_detection(camera_id: str) -> Response:
        """Return the latest frame with turtle detection overlay (persistent until next detection)."""
        payload = latest_frames.get(camera_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="No frame received")
        image = decode_jpeg(payload)
        orig_height, orig_width = image.shape[:2]
        detection = latest_detections.get(camera_id)
        detection_time = latest_detection_times.get(camera_id)
        # Apply the same transformations as _prepare_frame (before overlay)
        if camera_id != DOOR_CAMERA_ID:
            image = draw_enclosure_overlay(image)
            image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        else:
            image = draw_door_calibration_overlay(image)
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        # Draw overlay after transformations with adjusted coordinates
        history = detection_history.get(camera_id, [])
        if history:
            # Transform all detections in history
            transformed_history = []
            for det, det_time in history:
                transformed = _rotated_detection(det, camera_id, orig_width, orig_height)
                transformed_history.append((transformed, det_time))
            
            # Check if any detection should be shown
            show_overlay = False
            if camera_id == DOOR_CAMERA_ID:
                show_overlay = True
            elif any(in_enclosure_polygon(det.x_pixel, det.y_pixel, orig_width, orig_height) for det, _ in history):
                show_overlay = True
            
            if show_overlay:
                image = draw_detection_overlay(image, transformed_history)
        if camera_id != DOOR_CAMERA_ID and house_state["inside_house"]:
            house_detection = _rotated_detection(
                Detection(
                    x_pixel=settings.house_overlay_x_fraction * orig_width,
                    y_pixel=settings.house_overlay_y_fraction * orig_height,
                    confidence=1.0,
                ),
                camera_id,
                orig_width,
                orig_height,
            )
            image = draw_house_overlay(image, int(house_detection.x_pixel), int(house_detection.y_pixel))
        # Crop to enclosure for outdoor camera
        if camera_id != DOOR_CAMERA_ID:
            image = crop_to_enclosure(image)
        success, encoded = cv2.imencode(".jpg", image)
        if not success:
            raise HTTPException(status_code=500, detail="Could not encode frame with detection")
        return Response(content=encoded.tobytes(), media_type="image/jpeg")

    @app.get("/api/position", response_model=Position)
    def current_position() -> Position:
        row = database.latest_position()
        if row is None:
            raise HTTPException(status_code=404, detail="No position recorded")
        return Position(**{**row_to_dict(row), "inside_house": bool(row["inside_house"])})

    @app.get("/api/history", response_model=list[Position])
    def history(limit: int = 1000) -> list[Position]:
        if not 1 <= limit <= 10000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 10000")
        return [Position(**{**row_to_dict(row), "inside_house": bool(row["inside_house"])}) for row in database.positions(limit)]

    @app.get("/api/heatmap", response_model=list[HeatmapPoint])
    def heatmap(grid: float = 0.5) -> list[HeatmapPoint]:
        if grid <= 0:
            raise HTTPException(status_code=400, detail="grid must be positive")
        buckets: dict[tuple[int, int], int] = {}
        for row in database.positions(10000):
            key = (int(row["x"] / grid), int(row["y"] / grid))
            buckets[key] = buckets.get(key, 0) + 1
        return [HeatmapPoint(x=key[0] * grid, y=key[1] * grid, count=count) for key, count in buckets.items()]

    @app.get("/api/position/map", response_class=Response)
    def position_map() -> Response:
        row = database.latest_position()
        x = row["x"] if row is not None else None
        y = row["y"] if row is not None else None
        image = draw_position_map(
            settings.enclosure_length_meters,
            settings.enclosure_width_meters,
            x,
            y,
            house_state["inside_house"],
            settings.house_x_meters,
            settings.house_y_meters,
        )
        success, encoded = cv2.imencode(".jpg", image)
        if not success:
            raise HTTPException(status_code=500, detail="Could not encode position map")
        return Response(content=encoded.tobytes(), media_type="image/jpeg")

    @app.post("/api/frames/{camera_id}", response_model=IngestResponse)
    async def ingest_frame(camera_id: str, request: Request, file: UploadFile | None = File(None)) -> IngestResponse:
        payload = await file.read() if file is not None else await request.body()
        return await process_frame(camera_id, payload)

    @app.post("/api/mock/frame", response_model=IngestResponse)
    async def ingest_mock_frame() -> IngestResponse:
        if not settings.mock_images_enabled:
            raise HTTPException(status_code=404, detail="Mock images are disabled")
        return await process_frame("mock", mock_jpeg())

    @app.post("/api/radar/{camera_id}", response_model=IngestResponse)
    async def ingest_radar(camera_id: str, frame: RadarFrame) -> IngestResponse:
        if not camera_id.strip():
            raise HTTPException(status_code=400, detail="camera_id is required")
        if not frame.targets:
            return IngestResponse(accepted=False, reason="No radar target detected")
        target = frame.targets[0]
        x, y = radar_calibration.target_to_meters(target.x_mm, target.y_mm)
        timestamp = _utc_now()
        track = radar_tracker.update(x, y, timestamp)
        position = Position(
            timestamp=timestamp,
            x=track.x,
            y=track.y,
            inside_house=house_state["inside_house"],
            speed=track.speed,
            confidence=1.0,
            source="radar",
        )
        database.insert_position(
            timestamp.isoformat(), position.x, position.y, position.inside_house, position.speed, position.confidence, "radar"
        )
        mqtt_publisher.publish_position(position.x, position.y, position.speed, position.confidence)
        return IngestResponse(accepted=True, position=position)

    return app


app = create_app()
