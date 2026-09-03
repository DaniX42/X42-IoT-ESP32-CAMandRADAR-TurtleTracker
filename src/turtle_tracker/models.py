from datetime import datetime

from pydantic import BaseModel, Field


class Position(BaseModel):
    timestamp: datetime
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    inside_house: bool = False
    speed: float = Field(default=0, ge=0)
    confidence: float = Field(default=0, ge=0, le=1)
    source: str = "camera"


class IngestResponse(BaseModel):
    accepted: bool
    position: Position | None = None
    reason: str | None = None


class RadarTarget(BaseModel):
    """One HLK-LD2450 target report, in the sensor's native millimetre coordinates."""

    x_mm: int
    y_mm: int
    speed_mm_s: int = 0


class RadarFrame(BaseModel):
    targets: list[RadarTarget] = []


class HeatmapPoint(BaseModel):
    x: float
    y: float
    count: int


class MotionCrop(BaseModel):
    filename: str
    camera_id: str
    captured_at: datetime
    is_turtle: bool | None = None
    keep_for_training: bool = False


class MotionCropLabel(BaseModel):
    is_turtle: bool
    keep_for_training: bool = False


class MotionCropLabelItem(MotionCropLabel):
    filename: str


class MotionCropLabelBatch(BaseModel):
    items: list[MotionCropLabelItem]


class MotionCropPage(BaseModel):
    items: list[MotionCrop]
    total: int
    limit: int
    offset: int
