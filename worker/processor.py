"""The processing interface the worker runs jobs through.

``Processor`` is the only seam between the queue mechanics and the geometry library.
``PclappProcessor`` (worker/pclapp_processor.py) wraps the real ``pclapp`` package, which
needs PCL on the machine; ``tests/fake_processor.py`` is a pure-Python stand-in so the queue,
retry and concurrency tests run anywhere. The worker never imports ``pclapp`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class Summary:
    point_count: int
    bbox_min: Vec3 | None
    bbox_max: Vec3 | None
    centroid: Vec3 | None


@dataclass(frozen=True)
class ClusterInfo:
    size: int
    bbox_min: Vec3
    bbox_max: Vec3
    centroid: Vec3


class Processor(Protocol):
    def summarize(self, path: Path, *, has_intensity: bool) -> Summary:
        """Point count, bounding box and centroid of the cloud at ``path``."""

    def downsample(self, src: Path, dst: Path, *, has_intensity: bool, leaf_size: float) -> Summary:
        """Voxel-downsample ``src`` into ``dst`` (PCD) and return the result's summary."""

    def cluster(
        self, path: Path, *, has_intensity: bool, tolerance: float, min_size: int, max_size: int
    ) -> list[ClusterInfo]:
        """Euclidean clustering; one entry per cluster, largest first."""
