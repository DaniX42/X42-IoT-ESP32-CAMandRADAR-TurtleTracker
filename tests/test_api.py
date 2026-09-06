from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from turtle_tracker.app import _rotated_detection, create_app
from turtle_tracker.config import Settings
from turtle_tracker.db import Database
from turtle_tracker.mock import mock_jpeg
from turtle_tracker.vision import Detection, decode_jpeg


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_path=tmp_path / "test.db", motion_crops_path=tmp_path / "motion-crops")
    return TestClient(create_app(settings=settings, database=Database(settings.database_path)))


def test_health_and_empty_position(tmp_path: Path):
    client = make_client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/position").status_code == 404


def test_mock_frame_is_persisted(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post("/api/mock/frame")
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert client.get("/api/position").json()["x"] == body["position"]["x"]
    assert len(client.get("/api/history").json()) == 1


def test_radar_target_is_persisted(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post("/api/radar/turtle-cam-outdoor", json={"targets": [{"x_mm": 500, "y_mm": 2000, "speed_mm_s": 0}]})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["position"]["source"] == "radar"
    assert client.get("/api/position").json()["source"] == "radar"
    targets = client.get("/api/radar/turtle-cam-outdoor/targets")
    assert targets.status_code == 200
    assert targets.json()["targets"] == [{"x_mm": 500, "y_mm": 2000, "speed_mm_s": 0}]
    assert client.get("/api/radar/turtle-cam-outdoor/view").status_code == 200


def test_radar_without_targets_is_rejected(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post("/api/radar/turtle-cam-outdoor", json={"targets": []})
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_rejects_invalid_jpeg(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post("/api/frames/outdoor", files={"file": ("frame.jpg", b"invalid", "image/jpeg")})
    assert response.status_code == 415


def test_accepts_raw_jpeg_payload_from_camera(tmp_path: Path):
    client = make_client(tmp_path)
    response = client.post("/api/frames/outdoor", content=mock_jpeg(), headers={"content-type": "image/jpeg"})
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_detection_rotation_uses_actual_large_camera_resolution():
    detection = Detection(x_pixel=809, y_pixel=244, confidence=1.0)

    rotated = _rotated_detection(detection, "turtle-cam-outdoor", width=1600, height=1200)

    assert (rotated.x_pixel, rotated.y_pixel) == (244, 790)


def test_rejects_motion_outside_enclosure_polygon(tmp_path: Path):
    client = make_client(tmp_path)
    background = np.zeros((360, 640, 3), dtype=np.uint8)
    background[:] = (55, 105, 55)
    cv2.rectangle(background, (8, 8), (632, 352), (180, 180, 180), 2)
    ok, background_jpeg = cv2.imencode(".jpg", background)
    assert ok
    for _ in range(3):
        client.post("/api/frames/outdoor", content=background_jpeg.tobytes(), headers={"content-type": "image/jpeg"})

    response = client.post("/api/frames/outdoor", content=mock_jpeg(x=10, y=10), headers={"content-type": "image/jpeg"})

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert body["reason"] == "Motion detected outside the enclosure"


def test_outside_motion_keeps_last_valid_detection_overlay(tmp_path: Path):
    client = make_client(tmp_path)
    background = np.full((360, 640, 3), (55, 105, 55), dtype=np.uint8)
    cv2.rectangle(background, (8, 8), (632, 352), (180, 180, 180), 2)
    ok, background_jpeg = cv2.imencode(".jpg", background)
    assert ok
    for _ in range(3):
        client.post("/api/frames/outdoor", content=background_jpeg.tobytes(), headers={"content-type": "image/jpeg"})

    assert client.post("/api/frames/outdoor", content=mock_jpeg(), headers={"content-type": "image/jpeg"}).json()["accepted"] is True
    for _ in range(2):
        client.post("/api/frames/outdoor", content=background_jpeg.tobytes(), headers={"content-type": "image/jpeg"})
    outside = client.post("/api/frames/outdoor", content=mock_jpeg(x=10, y=10), headers={"content-type": "image/jpeg"})
    image = decode_jpeg(client.get("/api/frames/outdoor/latest/with-detection").content)
    marker = (image[:, :, 0] > 235) & (image[:, :, 1] > 235) & (image[:, :, 2] < 100)

    assert outside.json()["accepted"] is False
    assert marker.any()


def test_accepts_small_in_enclosure_motion_after_static_period(tmp_path: Path):
    client = make_client(tmp_path)
    background = np.full((360, 640, 3), (55, 105, 55), dtype=np.uint8)
    cv2.rectangle(background, (8, 8), (632, 352), (180, 180, 180), 2)
    stationary = background.copy()
    cv2.circle(stationary, (200, 180), 20, (35, 70, 130), -1)
    shifted = background.copy()
    cv2.circle(shifted, (205, 180), 20, (35, 70, 130), -1)
    ok, background_jpeg = cv2.imencode(".jpg", background)
    stationary_ok, stationary_jpeg = cv2.imencode(".jpg", stationary)
    shifted_ok, shifted_jpeg = cv2.imencode(".jpg", shifted)
    assert ok and stationary_ok and shifted_ok
    for _ in range(20):
        client.post("/api/frames/outdoor", content=background_jpeg.tobytes(), headers={"content-type": "image/jpeg"})
    for _ in range(150):
        client.post("/api/frames/outdoor", content=stationary_jpeg.tobytes(), headers={"content-type": "image/jpeg"})

    response = client.post("/api/frames/outdoor", content=shifted_jpeg.tobytes(), headers={"content-type": "image/jpeg"})

    assert response.json()["accepted"] is True


def test_latest_frame_is_available_as_jpeg(tmp_path: Path):
    client = make_client(tmp_path)
    payload = mock_jpeg()

    response = client.post("/api/frames/outdoor", content=payload, headers={"content-type": "image/jpeg"})

    assert response.status_code == 200
    latest = client.get("/api/frames/outdoor/latest")
    assert latest.status_code == 200
    assert latest.headers["content-type"] == "image/jpeg"
    # Image is cropped to enclosure, so height is less than 640
    height, width = decode_jpeg(latest.content).shape[:2]
    assert width == 360
    assert height < 640  # Cropped from top


def test_door_latest_frame_is_vga_with_overlay(tmp_path: Path):
    client = make_client(tmp_path)
    image = np.full((480, 640, 3), (40, 80, 160), dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", image)
    assert success

    response = client.post("/api/frames/turtle-cam-door", content=encoded.tobytes())

    assert response.status_code == 200
    latest = decode_jpeg(client.get("/api/frames/turtle-cam-door/latest").content)
    assert latest.shape[:2] == (640, 480)
    assert np.mean(np.abs(latest.astype(np.int16) - np.rot90(image, 3).astype(np.int16))) > 1


def test_latest_frame_square_keeps_full_image(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/frames/outdoor", content=mock_jpeg(), headers={"content-type": "image/jpeg"})

    square = client.get("/api/frames/outdoor/latest/square")

    assert square.status_code == 200
    assert square.headers["content-type"] == "image/jpeg"
    image = decode_jpeg(square.content)
    assert image.shape[:2] == (500, 500)


def test_latest_frame_square_jpg_alias_is_jpeg(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/frames/outdoor", content=mock_jpeg(), headers={"content-type": "image/jpeg"})

    response = client.get("/api/frames/outdoor/latest/square.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert decode_jpeg(response.content).shape[:2] == (500, 500)


def test_position_map_without_data_returns_jpeg(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/position/map")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_position_map_reflects_latest_position(tmp_path: Path):
    client = make_client(tmp_path)
    client.post("/api/mock/frame")

    response = client.get("/api/position/map")

    assert response.status_code == 200
    assert decode_jpeg(response.content) is not None


def test_motion_crop_review_includes_yes_no_toggle_overlay(tmp_path: Path):
    client = make_client(tmp_path)

    response = client.get("/api/motion-crops/review")

    assert response.status_code == 200
    html = response.text
    assert "YES" in html and "NO" in html
    assert "toggleCropSelection" in html
    assert "crop-status" in html
