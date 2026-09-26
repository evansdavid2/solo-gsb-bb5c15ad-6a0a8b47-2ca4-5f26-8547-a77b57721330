"""Tests for the sweep-line interval clustering of the three-way merge.

Independently added annotations whose intervals overlap across sides are
grouped (connected components of the bipartite overlap graph) into one
interval conflict.  ``app.merge._interval_clusters`` computes those
components with a time-boundary sweep instead of comparing every A/B pair
and storing every overlap edge; these tests pin the exact semantics
(half-open overlap, no same-side links, chained cross-side links, zero
length intervals, deterministic output order behind the conflict ids) and
verify equivalence with a naive pairwise reference on randomized inputs.

Run: python -m pytest backend/test_merge_interval_clusters.py
"""
import random
import time

from app.merge import _interval_clusters, analyze


def ANN(aid, s, e):
    return {"id": aid, "start": s, "end": e,
            "speaker": "", "topic": "", "note": ""}


def doc(anns=()):
    return {"segments": [], "annotations": list(anns)}


def naive_clusters(a_added, b_added):
    """Reference: every A/B pair compared, components via union-find."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for i, a in enumerate(a_added):
        for j, b in enumerate(b_added):
            if a["start"] < b["end"] and b["start"] < a["end"]:
                ra, rb = find(("a", i)), find(("b", j))
                if ra != rb:
                    parent[ra] = rb
    groups = {}
    for i in range(len(a_added)):
        groups.setdefault(find(("a", i)), [set(), set()])[0].add(i)
    for j in range(len(b_added)):
        groups.setdefault(find(("b", j)), [set(), set()])[1].add(j)
    clusters = [(sorted(a), sorted(b)) for a, b in groups.values() if a and b]
    clusters.sort(key=lambda cl: cl[0][0])  # by smallest A-side index
    return clusters


# ---------- semantics ----------

def test_touching_endpoints_do_not_connect():
    a = [ANN("a1", 0, 10)]
    b = [ANN("b1", 10, 20)]
    assert _interval_clusters(a, b) == []


def test_same_side_overlap_does_not_connect():
    # a1 and a2 overlap each other, but each links only its own B item:
    # two separate clusters, not one merged through the same-side overlap.
    a = [ANN("a1", 0, 10), ANN("a2", 5, 15)]
    b = [ANN("b1", 2, 3), ANN("b2", 12, 14)]
    assert _interval_clusters(a, b) == [([0], [0]), ([1], [1])]


def test_cross_side_chain_forms_one_cluster():
    a = [ANN("a1", 0, 6), ANN("a2", 8, 12)]
    b = [ANN("b1", 4, 9), ANN("b2", 100, 101)]
    assert _interval_clusters(a, b) == [([0, 1], [0])]


def test_component_merges_when_later_interval_bridges_it():
    # b3 starts after a2 failed to join the {a1, b1, b2} component, yet
    # overlaps members of both components -> everything ends up together.
    a = [ANN("a1", 0, 10), ANN("a2", 3, 100)]
    b = [ANN("b1", 1, 2), ANN("b2", 4, 5), ANN("b3", 6, 200)]
    assert _interval_clusters(a, b) == [([0, 1], [0, 1, 2])]


def test_component_stays_open_for_later_opposite_side():
    # {a1, b1} is still open (a1 runs to 10) when a2 starts; a2 overlaps
    # nothing and must not be absorbed.
    a = [ANN("a1", 0, 10), ANN("a2", 5, 15)]
    b = [ANN("b1", 2, 4)]
    assert _interval_clusters(a, b) == [([0], [0])]


def test_zero_length_interval_only_counts_when_strictly_inside():
    # [5,5] is strictly inside b1 -> cluster; it only touches b2's start.
    a = [ANN("a1", 5, 5)]
    assert _interval_clusters(a, [ANN("b1", 0, 10)]) == [([0], [0])]
    assert _interval_clusters(a, [ANN("b2", 5, 10)]) == []
    assert _interval_clusters(a, [ANN("b3", 0, 5)]) == []
    # two zero-length intervals never overlap, even at the same point
    assert _interval_clusters(a, [ANN("b4", 5, 5)]) == []


def test_same_start_batch_pairs_connect_only_when_both_nonzero():
    a = [ANN("a1", 4, 10), ANN("a2", 4, 4)]
    b = [ANN("b1", 4, 6)]
    assert _interval_clusters(a, b) == [([0], [0])]


def test_empty_side_yields_no_clusters():
    assert _interval_clusters([], [ANN("b1", 0, 1)]) == []
    assert _interval_clusters([ANN("a1", 0, 1)], []) == []
    assert _interval_clusters([], []) == []


# ---------- output contract (conflict ids and member order) ----------

def test_clusters_ordered_by_smallest_a_side_index_not_by_time():
    # ann-z starts earlier than ann-a, but a_added is id-sorted, so the
    # ann-a cluster comes first — the pairwise build's exact contract.
    a = [ANN("ann-a", 100, 110), ANN("ann-z", 0, 5)]
    b = [ANN("ann-b", 105, 108), ANN("ann-y", 1, 2)]
    _, conflicts, _ = analyze(doc(), doc(a), doc(b))
    assert [c["id"] for c in conflicts] == [
        "annotations:interval:0:ann-a", "annotations:interval:1:ann-z"]
    assert [x["id"] for x in conflicts[0]["aItems"]] == ["ann-a"]
    assert [x["id"] for x in conflicts[0]["bItems"]] == ["ann-b"]
    assert [x["id"] for x in conflicts[1]["aItems"]] == ["ann-z"]
    assert [x["id"] for x in conflicts[1]["bItems"]] == ["ann-y"]


def test_members_listed_in_id_sorted_index_order_not_time_order():
    a = [ANN("ann-b", 0, 10), ANN("ann-a", 5, 15)]  # id-sorted: ann-a first
    b = [ANN("ann-c", 8, 12)]
    _, conflicts, _ = analyze(doc(), doc(a), doc(b))
    (conf,) = conflicts
    assert conf["id"] == "annotations:interval:0:ann-a"
    assert [x["id"] for x in conf["aItems"]] == ["ann-a", "ann-b"]
    assert [x["id"] for x in conf["bItems"]] == ["ann-c"]


# ---------- randomized equivalence with the pairwise reference ----------

def test_sweep_matches_pairwise_reference_on_random_inputs():
    rng = random.Random(20260926)
    for _ in range(2000):
        hi = rng.choice([3, 10, 30, 100])
        a = [ANN(f"a{i}", *rand_interval(rng, hi))
             for i in range(rng.randint(0, 14))]
        b = [ANN(f"b{j}", *rand_interval(rng, hi))
             for j in range(rng.randint(0, 14))]
        assert _interval_clusters(a, b) == naive_clusters(a, b)


def rand_interval(rng, hi):
    s = rng.randint(0, hi)
    # frequent zero lengths and shared endpoints stress the edge cases
    return s, s + rng.choice([0, 0, 0, 1, 2, 3, 5, 8, 13])


# ---------- dense overlap regions scale ----------

def test_dense_overlap_region_avoids_pairwise_blowup():
    # ~200 intervals per side active at every moment: the pairwise build
    # compares and stores n*n edges here, the sweep stays near-linear.
    n = 4000
    a = [ANN(f"a{i}", i * 0.5, i * 0.5 + 100) for i in range(n)]
    b = [ANN(f"b{j}", j * 0.5 + 0.25, j * 0.5 + 100.25) for j in range(n)]
    started = time.perf_counter()
    clusters = _interval_clusters(a, b)
    elapsed = time.perf_counter() - started
    assert clusters == [(list(range(n)), list(range(n)))]
    assert elapsed < 10  # the sweep finishes in well under a second
