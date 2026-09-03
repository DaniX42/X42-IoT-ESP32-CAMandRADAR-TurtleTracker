from pathlib import Path

import cv2
import numpy as np


class HomographyCalibration:
    def __init__(self, image_points: list[list[float]], enclosure_length: float = 7.0, enclosure_width: float = 2.5):
        if len(image_points) != 4:
            raise ValueError("Exactly four image reference points are required")
        source = np.asarray(image_points, dtype=np.float32)
        target = np.asarray([[0, 0], [enclosure_length, 0], [enclosure_length, enclosure_width], [0, enclosure_width]], dtype=np.float32)
        matrix, _ = cv2.findHomography(source, target)
        if matrix is None:
            raise ValueError("Reference points cannot define a homography")
        self.matrix = matrix

    def pixel_to_meters(self, x_pixel: float, y_pixel: float) -> tuple[float, float]:
        point = np.asarray([[[x_pixel, y_pixel]]], dtype=np.float32)
        transformed = cv2.perspectiveTransform(point, self.matrix)[0, 0]
        return float(transformed[0]), float(transformed[1])


class RadarCalibration:
    """Maps HLK-LD2450 target coordinates (millimetres) to enclosure metres.

    The radar is mounted next to the outdoor camera at the same short enclosure edge,
    facing along the enclosure length. Its native Y axis (0-6000 mm, forward distance)
    becomes the enclosure length axis; its native X axis (-3000..3000 mm, left/right)
    becomes the enclosure width axis, centred on the sensor.
    """

    def __init__(
        self,
        enclosure_width: float = 2.5,
        offset_length_meters: float = 0.0,
        offset_width_meters: float = 0.0,
        mirror_width: bool = False,
    ):
        self.enclosure_width = enclosure_width
        self.offset_length_meters = offset_length_meters
        self.offset_width_meters = offset_width_meters
        self.mirror_width = mirror_width

    def target_to_meters(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        width_meters = x_mm / 1000.0
        if self.mirror_width:
            width_meters = -width_meters
        length = y_mm / 1000.0 + self.offset_length_meters
        width = self.enclosure_width / 2 + width_meters + self.offset_width_meters
        return length, width
