"""``Processor`` implemented with the real ``pclapp`` package (PCL underneath).

Imported only when the worker starts with ``PCLAPP_PROCESSOR=pclapp`` (the default), so the
API and the tests never need PCL installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pclapp

from worker.processor import ClusterInfo, Summary, Vec3


def _vec(values: Any) -> Vec3:
    x, y, z = (float(v) for v in values)
    return (x, y, z)


def _summary(cloud: Any) -> Summary:
    info = (
        cloud.summary()
    )  # {"point_count", "has_intensity", "bounding_box": {min,max}, "centroid"}
    box = info.get("bounding_box")
    return Summary(
        point_count=int(info["point_count"]),
        bbox_min=_vec(box["min"]) if box else None,
        bbox_max=_vec(box["max"]) if box else None,
        centroid=_vec(info["centroid"]) if info.get("centroid") is not None else None,
    )


class PclappProcessor:
    def summarize(self, path: Path, *, has_intensity: bool) -> Summary:
        return _summary(pclapp.load(path, intensity=has_intensity))

    def downsample(self, src: Path, dst: Path, *, has_intensity: bool, leaf_size: float) -> Summary:
        cloud = pclapp.load(src, intensity=has_intensity)
        small = pclapp.voxel_downsample(cloud, leaf_size=leaf_size)
        pclapp.save(small, dst)
        return _summary(small)

    def cluster(
        self, path: Path, *, has_intensity: bool, tolerance: float, min_size: int, max_size: int
    ) -> list[ClusterInfo]:
        cloud = pclapp.load(path, intensity=has_intensity)
        clusters = pclapp.euclidean_cluster(
            cloud, tolerance=tolerance, min_size=min_size, max_size=max_size
        )
        out: list[ClusterInfo] = []
        for c in clusters:
            s = _summary(c)
            if s.bbox_min is None or s.bbox_max is None or s.centroid is None:
                continue  # an empty cluster cannot come out of PCL, but keep mypy and the DB honest
            out.append(ClusterInfo(s.point_count, s.bbox_min, s.bbox_max, s.centroid))
        out.sort(key=lambda c: c.size, reverse=True)
        return out
