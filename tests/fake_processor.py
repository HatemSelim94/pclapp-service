"""A pure-Python ``Processor`` for tests, so the queue mechanics can be tested without PCL.

Reads whitespace-separated ``x y z [i]`` text whatever the file extension says, and writes
the same format. Deliberately simple algorithms: a real voxel grid (average per voxel) and
single-linkage clustering by BFS, O(n²), fine for the few hundred points tests use.

Lines that are not numbers raise ``ValueError`` — tests use that to produce failed jobs.
``FailingProcessor`` fails every call, for the retry test.
"""

from __future__ import annotations

import math
import time
from collections import deque
from pathlib import Path

from worker.processor import ClusterInfo, Summary, Vec3

Point = tuple[float, float, float]


def read_points(path: Path) -> list[Point]:
    points: list[Point] = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            raise ValueError(f"bad line: {line!r}")
        points.append((float(parts[0]), float(parts[1]), float(parts[2])))
    return points


def write_points(path: Path, points: list[Point]) -> None:
    path.write_text("".join(f"{x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in points))


def summarize(points: list[Point]) -> Summary:
    if not points:
        return Summary(0, None, None, None)
    n = len(points)
    mins: Vec3 = tuple(min(p[i] for p in points) for i in range(3))  # type: ignore[assignment]
    maxs: Vec3 = tuple(max(p[i] for p in points) for i in range(3))  # type: ignore[assignment]
    cen: Vec3 = tuple(sum(p[i] for p in points) / n for i in range(3))  # type: ignore[assignment]
    return Summary(n, mins, maxs, cen)


class FakeProcessor:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay  # lets concurrency tests make jobs overlap
        self.calls = 0

    def _tick(self) -> None:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)

    def summarize(self, path: Path, *, has_intensity: bool) -> Summary:
        self._tick()
        return summarize(read_points(path))

    def downsample(self, src: Path, dst: Path, *, has_intensity: bool, leaf_size: float) -> Summary:
        self._tick()
        voxels: dict[tuple[int, int, int], list[Point]] = {}
        for p in read_points(src):
            key = tuple(math.floor(c / leaf_size) for c in p)
            voxels.setdefault(key, []).append(p)  # type: ignore[arg-type]
        out = [
            (
                sum(p[0] for p in ps) / len(ps),
                sum(p[1] for p in ps) / len(ps),
                sum(p[2] for p in ps) / len(ps),
            )
            for ps in voxels.values()
        ]
        write_points(dst, out)
        return summarize(out)

    def cluster(
        self, path: Path, *, has_intensity: bool, tolerance: float, min_size: int, max_size: int
    ) -> list[ClusterInfo]:
        self._tick()
        pts = read_points(path)
        seen = [False] * len(pts)
        groups: list[list[Point]] = []
        tol2 = tolerance * tolerance
        for start in range(len(pts)):
            if seen[start]:
                continue
            seen[start] = True
            queue = deque([start])
            members = [pts[start]]
            while queue:
                i = queue.popleft()
                for j in range(len(pts)):
                    if not seen[j] and _dist2(pts[i], pts[j]) <= tol2:
                        seen[j] = True
                        queue.append(j)
                        members.append(pts[j])
            if min_size <= len(members) <= max_size:
                groups.append(members)
        groups.sort(key=len, reverse=True)
        result: list[ClusterInfo] = []
        for g in groups:
            s = summarize(g)
            assert s.bbox_min and s.bbox_max and s.centroid
            result.append(ClusterInfo(len(g), s.bbox_min, s.bbox_max, s.centroid))
        return result


class FailingProcessor(FakeProcessor):
    def _tick(self) -> None:
        super()._tick()
        raise RuntimeError("boom")


def _dist2(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
