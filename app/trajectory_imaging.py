"""Display periodic images of bonded fragments, independently for each saved frame."""

from dataclasses import dataclass

import numpy as np


def cell(box_nm):
    if box_nm is None:
        return None
    box = np.asarray(box_nm, dtype=float) * 10
    if box.shape != (3, 3) or not np.isfinite(box).all() or np.linalg.det(box) <= 1e-6 or np.linalg.cond(box) > 1e5:
        return None
    return box, np.linalg.inv(box)


def short_vector(delta, box):
    return delta if box is None else delta - np.rint(delta @ box[1]) @ box[0]


def nearby(reference, query, box, k=1):
    """Bounded nearest candidates; measure distances in Cartesian Å even in skewed cells."""
    from scipy.spatial import cKDTree

    if box is None:
        return cKDTree(reference).query(query, k=k)

    # Search periodic fractional space; compare several candidates in Cartesian space.
    def fractional(xyz):
        return np.minimum((xyz @ box[1]) % 1, np.nextafter(1.0, 0.0))

    tree = cKDTree(fractional(reference), boxsize=1)
    count = min(max(k, 8), len(reference))
    _, near = tree.query(fractional(query), k=list(range(1, count + 1)))
    dist = np.linalg.norm(short_vector(query[:, None] - reference[near], box), axis=-1)
    if k == 1:
        best = np.argmin(dist, axis=1)
        rows = np.arange(len(query))
        return dist[rows, best], near[rows, best]
    return dist, near


@dataclass
class FragmentImages:
    parent: np.ndarray
    ancestors: list
    groups: np.ndarray
    counts: np.ndarray
    anchor_group: int | None
    reference_center: np.ndarray | None = None

    @classmethod
    def build(cls, count, edges, anchor):
        # Build a spanning forest once. Precomputed ancestor jumps sum lattice translations in O(log depth) vector operations.
        neighbors: list[list[int]] = [[] for _ in range(count)]
        for a, b in edges:
            neighbors[a].append(b)
            neighbors[b].append(a)
        parent = np.full(count, -1, dtype=np.intp)
        for root in range(count):
            if parent[root] >= 0:
                continue
            parent[root] = root
            stack = [root]
            while stack:
                a = stack.pop()
                for b in neighbors[a]:
                    if parent[b] < 0:
                        parent[b] = a
                        stack.append(b)
        ancestors = []
        anc = parent
        while not np.array_equal(anc, anc[anc]):
            ancestors.append(anc)
            anc = anc[anc]
        _, groups, counts = np.unique(anc, return_inverse=True, return_counts=True)
        return cls(parent, ancestors, groups, counts, None if anchor is None else int(groups[anchor]))

    def apply(self, xyz, box, *, reference=False):
        if box is None:
            return xyz
        shifts = np.rint((xyz[self.parent] - xyz) @ box[1])
        for anc in self.ancestors:
            shifts += shifts[anc]
        whole = xyz + shifts @ box[0]
        if self.anchor_group is None:
            return whole
        centers = np.stack([np.bincount(self.groups, weights=dim) / self.counts for dim in whole.T], axis=1)
        center = centers[self.anchor_group]
        if reference:
            self.reference_center = center.copy()
        whole -= (np.rint((centers - center) @ box[1]) @ box[0])[self.groups]
        if self.reference_center is not None:
            whole += self.reference_center - center  # follow protein translation, without fitting rotation
        return whole
