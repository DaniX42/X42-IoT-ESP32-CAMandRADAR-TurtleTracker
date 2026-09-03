from dataclasses import dataclass
from datetime import datetime

from .calibration import HomographyCalibration
from .vision import Detection


@dataclass
class Track:
    x: float
    y: float
    timestamp: datetime
    speed: float = 0.0


def _clamp_and_track(
    previous: Track | None, x: float, y: float, timestamp: datetime, enclosure_length: float, enclosure_width: float
) -> Track:
    # Clamp coordinates to valid enclosure range
    x = min(max(x, 0.0), enclosure_length)
    y = min(max(y, 0.0), enclosure_width)
    speed = 0.0
    if previous:
        elapsed = max((timestamp - previous.timestamp).total_seconds(), 0.001)
        speed = ((x - previous.x) ** 2 + (y - previous.y) ** 2) ** 0.5 / elapsed
    return Track(x, y, timestamp, speed)


class PositionTracker:
    def __init__(self, calibration: HomographyCalibration, enclosure_length: float = 7.0, enclosure_width: float = 2.5):
        self.calibration = calibration
        self.enclosure_length = enclosure_length
        self.enclosure_width = enclosure_width
        self.previous: Track | None = None

    def update(self, detection: Detection, timestamp: datetime) -> Track:
        x, y = self.calibration.pixel_to_meters(detection.x_pixel, detection.y_pixel)
        track = _clamp_and_track(self.previous, x, y, timestamp, self.enclosure_length, self.enclosure_width)
        self.previous = track
        return track


class RadarPositionTracker:
    """Tracks a moving point given directly in enclosure metres, e.g. from a radar sensor."""

    def __init__(self, enclosure_length: float = 7.0, enclosure_width: float = 2.5):
        self.enclosure_length = enclosure_length
        self.enclosure_width = enclosure_width
        self.previous: Track | None = None

    def update(self, x: float, y: float, timestamp: datetime) -> Track:
        track = _clamp_and_track(self.previous, x, y, timestamp, self.enclosure_length, self.enclosure_width)
        self.previous = track
        return track


@dataclass
class DoorCrossing:
    event: str  # "entered_house" or "left_house"
    timestamp: datetime


class DoorCrossingTracker:
    """Tracks which side of the door threshold the tortoise is on and reports crossings."""

    def __init__(self) -> None:
        self.last_side: str | None = None

    def update(self, side: str | None, timestamp: datetime) -> DoorCrossing | None:
        if side is None:
            return None
        crossing = None
        if self.last_side is not None and side != self.last_side:
            event = "entered_house" if side == "inside" else "left_house"
            crossing = DoorCrossing(event, timestamp)
        self.last_side = side
        return crossing
