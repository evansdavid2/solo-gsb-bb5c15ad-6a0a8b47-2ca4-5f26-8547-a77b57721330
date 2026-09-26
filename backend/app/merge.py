"""Three-way merge for annotated transcript documents.

A document is {"segments": [...], "annotations": [...]}, every item carries
a stable client-generated ``id``.  Two editors branch from a common *base*,
edit offline, and their copies are merged:

* changes touching different items are merged automatically;
* when both sides change the same item differently -> ``identity`` conflict;
* modify-vs-delete of the same item -> ``identity`` conflict;
* annotations independently added by the two sides whose intervals overlap
  are grouped (connected components of the overlap graph) into one
  ``interval`` conflict;
* each conflict is adjudicated per item; resolutions may pick a side,
  supply a custom value, drop the item(s), or (interval only) keep all.

Before any index is built the three input documents are validated
(:func:`validate_documents`): every item id must be a non-empty string,
unique within its own side + collection — otherwise later items with the
same id would silently overwrite earlier ones in the index (records lost)
or a missing id would crash the merge.  Validation raises
:class:`MergeInputError` listing side, collection, row and id of every
problem, and no merge result is produced.  The same id across sides is
exactly how items are matched, and a segment and an annotation on the
same side may share an id — only duplicates *within* one side's
collection are rejected, along with missing / non-string ids.

Beyond the core fields (``start`` / ``end`` / ``speaker`` / ``text`` /
``topic`` / ``note``) annotations carry optional rule provenance fields —
``color``, ``source``, ``ruleId``, ``ruleSlot``, ``ruleName``.  They take
part in the comparison and the merge **by presence as well as value**: a
side that recolors an annotation, or detaches it from its rule by removing
``source`` / ``ruleName``, makes a real change that the merge must keep even
when the other side only edits another field.  Every adjudication mode
(whole side, per-field, custom) preserves these fields, and a detached
annotation stays a manual item when its old rule is re-run.
"""
from __future__ import annotations

from copy import deepcopy
from heapq import heappop, heappush

SEG_KEY = "segments"
ANN_KEY = "annotations"

SEG_FIELDS = ("start", "end", "speaker", "text")
ANN_FIELDS = ("start", "end", "speaker", "topic", "note")
# Optional annotation metadata (color + rule provenance).  Presence itself
# is meaningful: removing ``source`` detaches the annotation from its rule.
ANN_EXTRA_FIELDS = ("color", "source", "ruleId", "ruleSlot", "ruleName")
_ITEM_FIELDS = {SEG_KEY: SEG_FIELDS, ANN_KEY: ANN_FIELDS}
_EXTRA_FIELDS = {SEG_KEY: (), ANN_KEY: ANN_EXTRA_FIELDS}

# Sentinel for "field absent from this item" (distinct from a stored None).
MISSING = object()

# Kept in sync with rules.DEFAULT_COLOR without importing the rule engine
# (the merge module stays dependency-free).
DEFAULT_ANN_COLOR = "#5b8def"

# Side / collection labels used in the input-validation error messages.
SIDE_LABELS = {"base": "基准（Base）", "a": "副本 A", "b": "副本 B"}
COLLECTION_LABELS = {SEG_KEY: "segments（片段）",
                     ANN_KEY: "annotations（校注）"}


class MergeInputError(ValueError):
    """Invalid item ids in the merge input documents.

    ``problems`` is a list of dicts::

        {"side": "base" | "a" | "b",
         "collection": "segments" | "annotations",
         "reason": "missing" | "invalid" | "duplicate",
         "indexes": [<0-based row>, ...],
         "id": <the repeated / ill-typed id> or None}

    Raised before any index is built: an input with duplicate or missing
    ids must never silently lose records or crash the merge, and no merge
    result is produced for it.
    """

    def __init__(self, problems):
        self.problems = problems
        super().__init__(_describe_input_problems(problems))


def _describe_input_problems(problems):
    lines = ["输入文档存在无效的记录 ID，未执行合并（不会生成合并结果）："]
    for p in problems:
        rows = "、".join(str(i + 1) for i in p["indexes"])
        where = (f"{SIDE_LABELS[p['side']]} 的 "
                 f"{COLLECTION_LABELS[p['collection']]}第 {rows} 条")
        if p["reason"] == "duplicate":
            lines.append(
                f"{where}：ID「{p['id']}」重复（同一侧的同类记录中 ID 必须唯一）")
        elif p["reason"] == "invalid":
            lines.append(
                f"{where}：ID 必须是非空字符串，实际值为 {p['id']!r}")
        else:
            lines.append(
                f"{where}：记录 ID 缺失或为空（每条记录必须携带非空字符串 ID）")
    return "\n".join(lines)


def _validate_side_ids(doc, side):
    """Id problems within one side's own segments / annotations."""
    problems = []
    for kind in (SEG_KEY, ANN_KEY):
        items = (doc or {}).get(kind) or []
        first_row = {}
        duplicates = {}
        for i, item in enumerate(items):
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id is None or (
                    isinstance(item_id, str) and not item_id.strip()):
                problems.append({
                    "side": side, "collection": kind, "reason": "missing",
                    "indexes": [i], "id": None,
                })
            elif not isinstance(item_id, str):
                problems.append({
                    "side": side, "collection": kind, "reason": "invalid",
                    "indexes": [i], "id": item_id,
                })
            elif item_id in first_row:
                dup = duplicates.get(item_id)
                if dup is None:
                    dup = duplicates[item_id] = {
                        "side": side, "collection": kind,
                        "reason": "duplicate",
                        "indexes": [first_row[item_id]], "id": item_id,
                    }
                dup["indexes"].append(i)
            else:
                first_row[item_id] = i
        problems.extend(duplicates.values())
    return problems


def validate_documents(base_doc, a_doc, b_doc):
    """Validate item ids of all three sides before any index is built.

    Every segment / annotation id must be a non-empty string, unique
    within its own side + collection.  The same id across sides (the
    normal case — that is how items are matched) or shared between a
    side's segments and its annotations stays valid.  Raises
    :class:`MergeInputError` listing every problem found.
    """
    problems = []
    for side, doc in (("base", base_doc), ("a", a_doc), ("b", b_doc)):
        problems.extend(_validate_side_ids(doc, side))
    if problems:
        raise MergeInputError(problems)


def _index(items):
    return {it["id"]: it for it in (items or [])}


def _fields(kind):
    return _ITEM_FIELDS[kind]


def _extra_fields(kind):
    return _EXTRA_FIELDS[kind]


def _all_fields(kind):
    return _ITEM_FIELDS[kind] + _EXTRA_FIELDS[kind]


def _get(item, field):
    """Presence-aware field read: MISSING when the key is absent."""
    return item.get(field, MISSING) if item is not None else MISSING


def _put(target, field, value):
    """Write a presence-aware value: MISSING removes the key."""
    if value is MISSING:
        target.pop(field, None)
    else:
        target[field] = value


def _equal(x, y, kind):
    return all(_get(x, f) == _get(y, f) for f in _all_fields(kind))


def _overlap(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def _identity_conflict(kind, item_id, base, a, b, ctype):
    """Both sides diverged on the same id."""
    fields = _all_fields(kind)
    if ctype == "modify":
        conflicting = []
        partial = {"id": item_id}
        for f in fields:
            av, bv, bv0 = _get(a, f), _get(b, f), _get(base, f)
            a_changed, b_changed = av != bv0, bv != bv0
            if a_changed and b_changed and av != bv:
                conflicting.append(f)
            elif a_changed:
                _put(partial, f, av)
            elif b_changed:
                _put(partial, f, bv)
            else:
                _put(partial, f, bv0)
        if not conflicting:
            return None, partial  # field-level edits don't overlap
        changed = sorted(
            f for f in fields
            if _get(a, f) != _get(base, f) or _get(b, f) != _get(base, f)
        )
        conf = {
            "id": f"{kind}:{item_id}:{ctype}",
            "kind": kind,
            "conflictType": "identity",
            "itemId": item_id,
            "modifyDelete": False,
            "base": deepcopy(base),
            "a": deepcopy(a),
            "b": deepcopy(b),
            "changedFields": changed,
            "conflictingFields": conflicting,
            "partialMerge": partial,
            "resolution": None,
        }
        return conf, None

    survivor = a if a is not None else b
    changed = sorted(
        f for f in fields if _get(survivor, f) != _get(base, f))
    return {
        "id": f"{kind}:{item_id}:{ctype}",
        "kind": kind,
        "conflictType": "identity",
        "itemId": item_id,
        "modifyDelete": True,
        "base": deepcopy(base),
        "a": deepcopy(a),
        "b": deepcopy(b),
        "changedFields": changed,
        "conflictingFields": changed,
        "partialMerge": None,
        "resolution": None,
    }, None


def _interval_clusters(a_added, b_added):
    """Connected components of the bipartite overlap graph.

    Sweep-line over the time axis: intervals are processed in start-time
    order (equal starts form one batch) and only the state still needed to
    grow components is kept — each open component's member lists plus its
    latest A-side / B-side end, indexed in two heaps.  A new interval joins
    exactly the components whose opposite-side end lies past its start, so
    a dense overlap region costs O(log n) per interval instead of comparing
    every A/B pair and storing every overlap edge.  The semantics are
    unchanged: half-open overlap (:func:`_overlap`), touching endpoints
    never connect, same-side overlaps never connect, and cross-side chains
    land in one cluster.  The returned clusters are ordered by their
    smallest A-side index with both sides' indices ascending, exactly as
    the pairwise construction produced.
    """
    if not a_added or not b_added:
        return []

    # (start, side, index) — side 0 = A, 1 = B; equal starts are processed
    # as one batch so same-start cross-side overlaps are judged exactly.
    events = [(x["start"], 0, i) for i, x in enumerate(a_added)]
    events += [(x["start"], 1, j) for j, x in enumerate(b_added)]
    events.sort(key=lambda e: e[0])

    NEG_INF = float("-inf")
    comps = []   # cid -> [maxAEnd, maxBEnd, aMembers, bMembers] or None
    heap_a = []  # (-maxAEnd, cid): components a new B interval may join
    heap_b = []  # (-maxBEnd, cid): components a new A interval may join

    def _sweep(heap, side, s):
        """Pop the live components whose side-``side`` end passes ``s``."""
        hit = []
        while heap:
            neg_end, cid = heap[0]
            if -neg_end <= s:
                break
            heappop(heap)
            comp = comps[cid]
            # skip entries of absorbed components or superseded end values
            if comp is not None and comp[side] == -neg_end:
                hit.append(cid)
        return hit

    pos = 0
    while pos < len(events):
        s = events[pos][0]
        batch_a, batch_b = [], []
        while pos < len(events) and events[pos][0] == s:
            _, side, idx = events[pos]
            (batch_a if side == 0 else batch_b).append(idx)
            pos += 1

        # An interval starting at s strictly overlaps an opposite-side
        # member m iff m.end > s: every component member comes from an
        # earlier batch, so m.start < s <= the interval's end already
        # holds (zero-length intervals included).
        join_a = _sweep(heap_b, 1, s) if batch_a else []
        join_b = _sweep(heap_a, 0, s) if batch_b else []
        popped_from_a = set(join_b)  # heap_a entries removed this batch
        popped_from_b = set(join_a)  # heap_b entries removed this batch

        # Union everything this batch connects: every A interval joins
        # every join_a component, every B interval joins every join_b
        # component, and same-batch A/B pairs both ending past s overlap.
        parent = {}

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for cid in join_a + join_b:
            parent.setdefault(("c", cid), ("c", cid))
        for i in batch_a:
            parent.setdefault(("a", i), ("a", i))
        for j in batch_b:
            parent.setdefault(("b", j), ("b", j))
        if join_a:
            hub = ("c", join_a[0])
            for cid in join_a[1:]:
                union(hub, ("c", cid))
            for i in batch_a:
                union(hub, ("a", i))
        if join_b:
            hub = ("c", join_b[0])
            for cid in join_b[1:]:
                union(hub, ("c", cid))
            for j in batch_b:
                union(hub, ("b", j))
        wide_a = [i for i in batch_a if a_added[i]["end"] > s]
        wide_b = [j for j in batch_b if b_added[j]["end"] > s]
        if wide_a and wide_b:
            # same-batch cross-side clique: all pairs ending past s overlap
            root_a = ("c", join_a[0]) if join_a else ("a", wide_a[0])
            if not join_a:
                for i in wide_a[1:]:
                    union(root_a, ("a", i))
            root_b = ("c", join_b[0]) if join_b else ("b", wide_b[0])
            if not join_b:
                for j in wide_b[1:]:
                    union(root_b, ("b", j))
            union(root_a, root_b)

        # Materialize each connected group as a single component.
        groups = {}
        for cid in join_a + join_b:
            groups.setdefault(find(("c", cid)), [[], [], []])[0].append(cid)
        for i in batch_a:
            groups.setdefault(find(("a", i)), [[], [], []])[1].append(i)
        for j in batch_b:
            groups.setdefault(find(("b", j)), [[], [], []])[2].append(j)

        for cids, ais, bis in groups.values():
            max_a_end = max([comps[c][0] for c in cids]
                            + [a_added[i]["end"] for i in ais] + [NEG_INF])
            max_b_end = max([comps[c][1] for c in cids]
                            + [b_added[j]["end"] for j in bis] + [NEG_INF])
            if not cids and max(max_a_end, max_b_end) <= s:
                continue  # zero-length singleton: can never gain a partner
            if cids:
                # The largest component survives and absorbs the rest, so
                # member lists move O(log n) times per interval overall.
                keep = max(cids,
                           key=lambda c: len(comps[c][2]) + len(comps[c][3]))
                comp = comps[keep]
                old_a_end, old_b_end = comp[0], comp[1]
                for c in cids:
                    if c == keep:
                        continue
                    comp[2].extend(comps[c][2])
                    comp[3].extend(comps[c][3])
                    comps[c] = None
                comp[2].extend(ais)
                comp[3].extend(bis)
                comp[0], comp[1] = max_a_end, max_b_end
                # Re-index the survivor: entries popped above are gone and
                # grown end values supersede the ones still in the heaps.
                if keep in popped_from_a or max_a_end != old_a_end:
                    heappush(heap_a, (-max_a_end, keep))
                if keep in popped_from_b or max_b_end != old_b_end:
                    heappush(heap_b, (-max_b_end, keep))
            else:
                cid = len(comps)
                comps.append([max_a_end, max_b_end, list(ais), list(bis)])
                if max_a_end != NEG_INF:
                    heappush(heap_a, (-max_a_end, cid))
                if max_b_end != NEG_INF:
                    heappush(heap_b, (-max_b_end, cid))

    clusters = [comp for comp in comps
                if comp is not None and comp[2] and comp[3]]
    # Output contract of the pairwise construction: clusters ordered by
    # smallest A-side index, each side's indices ascending.
    clusters.sort(key=lambda comp: min(comp[2]))
    return [(sorted(comp[2]), sorted(comp[3])) for comp in clusters]


def analyze(base_doc, a_doc, b_doc):
    """Return (changes, conflicts, stats)."""
    base_doc, a_doc, b_doc = base_doc or {}, a_doc or {}, b_doc or {}
    # Reject duplicate / missing ids up front: indexing would otherwise
    # silently drop records or crash.  No merge result is produced.
    validate_documents(base_doc, a_doc, b_doc)
    changes = []
    conflicts = []
    stats = {
        "segments": {"added": 0, "modified": 0, "deleted": 0},
        "annotations": {"added": 0, "modified": 0, "deleted": 0},
        "conflicts": 0,
    }

    for kind in (SEG_KEY, ANN_KEY):
        base_idx = _index(base_doc.get(kind, []))
        a_idx = _index(a_doc.get(kind, []))
        b_idx = _index(b_doc.get(kind, []))
        a_added, b_added = [], []

        for item_id in sorted(set(base_idx) | set(a_idx) | set(b_idx)):
            base, a, b = (d.get(item_id) for d in (base_idx, a_idx, b_idx))
            in_b, in_a, in_bb = base is not None, a is not None, b is not None

            if in_b and in_a and in_bb:
                if a == b:
                    if not _equal(base, a, kind):
                        stats[kind]["modified"] += 1
                        changes.append(_ch(kind, item_id, "modified", a))
                    else:
                        changes.append(_ch(kind, item_id, "unchanged", a))
                elif _equal(base, a, kind):
                    stats[kind]["modified"] += 1
                    changes.append(_ch(kind, item_id, "modified", b))
                elif _equal(base, b, kind):
                    stats[kind]["modified"] += 1
                    changes.append(_ch(kind, item_id, "modified", a))
                else:
                    conf, partial = _identity_conflict(
                        kind, item_id, base, a, b, "modify")
                    if conf is None:
                        # both sides edited, but disjoint fields -> field merge
                        stats[kind]["modified"] += 1
                        changes.append(_ch(kind, item_id, "modified", partial))
                    else:
                        conflicts.append(conf)
                        changes.append(_conflict_ch(kind, item_id, conf))

            elif in_b and in_a:  # B deleted
                if _equal(base, a, kind):
                    stats[kind]["deleted"] += 1
                    changes.append(_ch(kind, item_id, "deleted", None))
                else:
                    conf, _ = _identity_conflict(
                        kind, item_id, base, a, None, "modifyDelete")
                    conflicts.append(conf)
                    changes.append(_conflict_ch(kind, item_id, conf))

            elif in_b and in_bb:  # A deleted
                if _equal(base, b, kind):
                    stats[kind]["deleted"] += 1
                    changes.append(_ch(kind, item_id, "deleted", None))
                else:
                    conf, _ = _identity_conflict(
                        kind, item_id, base, None, b, "modifyDelete")
                    conflicts.append(conf)
                    changes.append(_conflict_ch(kind, item_id, conf))

            elif in_b:  # both deleted
                stats[kind]["deleted"] += 1
                changes.append(_ch(kind, item_id, "deleted", None))

            elif in_a and in_bb:  # both added with the same id
                if a == b:
                    stats[kind]["added"] += 1
                    changes.append(_ch(kind, item_id, "added", a))
                else:
                    conf = {
                        "id": f"{kind}:{item_id}:addadd",
                        "kind": kind,
                        "conflictType": "identity",
                        "itemId": item_id,
                        "modifyDelete": False,
                        "base": None,
                        "a": deepcopy(a),
                        "b": deepcopy(b),
                        "changedFields": sorted(
                            f for f in _all_fields(kind)
                            if _get(a, f) != _get(b, f)
                        ),
                        "conflictingFields": sorted(
                            f for f in _all_fields(kind)
                            if _get(a, f) != _get(b, f)
                        ),
                        "partialMerge": None,
                        "resolution": None,
                    }
                    conflicts.append(conf)
                    changes.append(_conflict_ch(kind, item_id, conf))

            elif in_a:
                stats[kind]["added"] += 1
                a_added.append(a)
                changes.append(_ch(kind, item_id, "added", a, side="a"))
            elif in_bb:
                stats[kind]["added"] += 1
                b_added.append(b)
                changes.append(_ch(kind, item_id, "added", b, side="b"))

        if kind == ANN_KEY:
            for n, (ai, bj) in enumerate(_interval_clusters(a_added, b_added)):
                a_items = [deepcopy(a_added[i]) for i in ai]
                b_items = [deepcopy(b_added[j]) for j in bj]
                anchor = a_items[0]["id"]
                conf = {
                    "id": f"{ANN_KEY}:interval:{n}:{anchor}",
                    "kind": ANN_KEY,
                    "conflictType": "interval",
                    "itemId": None,
                    "aItems": a_items,
                    "bItems": b_items,
                    "base": None,
                    "a": a_items[0],
                    "b": b_items[0],
                    "changedFields": ["start", "end"],
                    "resolution": None,
                }
                conflicts.append(conf)
                changes.append({
                    "kind": ANN_KEY,
                    "action": "conflict",
                    "itemId": None,
                    "conflictId": conf["id"],
                    "aItems": deepcopy(a_items),
                    "bItems": deepcopy(b_items),
                })

    stats["conflicts"] = len(conflicts)
    return changes, conflicts, stats


def _ch(kind, item_id, action, merged, side=None):
    ch = {"kind": kind, "itemId": item_id, "action": action,
          "merged": deepcopy(merged)}
    if side:
        ch["side"] = side
    return ch


def _conflict_ch(kind, item_id, conf):
    return {
        "kind": kind, "itemId": item_id, "action": "conflict",
        "conflictId": conf["id"],
        "base": deepcopy(conf["base"]),
        "a": deepcopy(conf["a"]),
        "b": deepcopy(conf["b"]),
    }


def build_merged(changes, conflicts, resolutions):
    """Materialize the merged document from changes + adjudications.

    resolutions: {conflictId: {"choice": ..., "value"?: item}}
      identity conflict: a | b | custom | drop
      interval conflict: a | b | custom | keep | drop
    Returns (doc, unresolved_ids, errors).
    """
    resolutions = resolutions or {}
    unresolved = [c["id"] for c in conflicts if c["id"] not in resolutions]
    errors = []

    items = {}  # (kind, itemId) -> item or None
    for ch in changes:
        if ch["action"] != "conflict":
            items[(ch["kind"], ch["itemId"])] = ch.get("merged")

    for c in conflicts:
        res = resolutions.get(c["id"])
        if res is None:
            continue
        try:
            _apply_resolution(c, res, items)
        except ValueError as exc:
            errors.append({"conflictId": c["id"], "message": str(exc)})

    doc = {SEG_KEY: [], ANN_KEY: []}
    for (kind, _id), value in items.items():
        if value is not None:
            doc[kind].append(value)
    for key in (SEG_KEY, ANN_KEY):
        doc[key].sort(key=lambda x: (x.get("start", 0.0), str(x.get("id", ""))))
    return doc, unresolved, errors


def _apply_resolution(conf, res, items):
    kind = conf["kind"]
    choice = res.get("choice")

    if conf["conflictType"] == "interval":
        a_ids = {x["id"] for x in conf["aItems"]}
        b_ids = {x["id"] for x in conf["bItems"]}
        if choice == "a":
            for i in b_ids:
                items.pop((ANN_KEY, i), None)
        elif choice == "b":
            for i in a_ids:
                items.pop((ANN_KEY, i), None)
        elif choice == "keep":
            pass  # retain every item, overlap accepted
        elif choice == "drop":
            for i in a_ids | b_ids:
                items.pop((ANN_KEY, i), None)
        elif choice == "custom":
            value = _validate_custom(
                conf, _inherit_extras(conf, dict(res.get("value") or {})))
            for i in a_ids | b_ids:
                items.pop((ANN_KEY, i), None)
            items[(ANN_KEY, value["id"])] = value
        else:
            raise ValueError(f"未知裁决方式: {choice!r}")
        return

    item_id = conf["itemId"]
    fields = _all_fields(kind)
    if choice in ("a", "b"):
        value = conf[choice]
        if value is None:
            items.pop((kind, item_id), None)
        else:
            items[(kind, item_id)] = deepcopy(value)
    elif choice == "drop":
        items.pop((kind, item_id), None)
    elif choice == "custom":
        value = _validate_custom(
            conf, _inherit_extras(conf, dict(res.get("value") or {})))
        items[(kind, value["id"])] = value
    elif choice == "fields":
        # per-field adjudication: res["fields"] = {fieldName: "a"|"b"};
        # fields not listed there come from the automatic partial merge.
        chosen_fields = res.get("fields") or {}
        partial = dict(conf.get("partialMerge") or {})
        for f in fields:
            side = chosen_fields.get(f)
            if side in ("a", "b"):
                src = conf[side]
                if src is not None:
                    _put(partial, f, _get(src, f))
            elif f not in partial:
                src = conf["a"] if conf["a"] is not None else conf["b"]
                _put(partial, f, _get(src or {}, f))
        partial = _validate_custom(conf, partial)
        items[(kind, item_id)] = partial
    else:
        raise ValueError(f"未知裁决方式: {choice!r}")


def _clean_extra(field, raw, present):
    """Validate one optional annotation metadata value.

    ``present`` distinguishes an absent key (returns MISSING, so a detached
    annotation stays detached) from an explicit ``null`` / empty value
    (normalized to a benign default instead of crashing the merge).
    """
    if not present or raw is None:
        if field == "color":
            return DEFAULT_ANN_COLOR if present else MISSING
        return MISSING
    if field == "ruleSlot":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("ruleSlot 必须是整数")
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"{field} 必须是字符串")
    if field == "color":
        return raw.strip() or DEFAULT_ANN_COLOR
    return raw


def _inherit_extras(conf, value):
    """Fill extra fields a custom value omitted.

    Rules, mirroring the per-field adjudication semantics:

    * a field both sides changed to different values (it is in
      ``conflictingFields``) defaults to A — the same default the
      per-field UI shows before an explicit pick;
    * every other field is taken from the automatic partial merge, which is
      authoritative **including key absence**: when the detaching side
      removed ``source`` / ``ruleName`` the partial lacks those keys and the
      custom value must not re-attach them;
    * conflicts without a partial merge (add/add, interval) have no base, so
      equal fields are taken from A directly.
    """
    partial = conf.get("partialMerge")
    conflicting = set(conf.get("conflictingFields") or ())
    for f in _extra_fields(conf["kind"]):
        if f in value:
            continue
        if f in conflicting:
            donor = conf.get("a") or conf.get("b") or {}
        elif isinstance(partial, dict):
            donor = partial
        else:
            donor = conf.get("a") or conf.get("b") or {}
        try:
            cleaned = _clean_extra(f, donor.get(f), present=f in donor)
        except ValueError:
            continue
        if cleaned is not MISSING:
            value[f] = cleaned
    return value


def _validate_custom(conf, value):
    kind = conf["kind"]
    fields = _fields(kind)
    if conf["conflictType"] == "interval":
        item_id = value.get("id") or f"custom-{conf['id']}"
    else:
        item_id = conf["itemId"]
    clean = {"id": item_id}
    for f in fields:
        if f not in value:
            raise ValueError(f"缺少字段 {f}")
        v = value[f]
        if f in ("start", "end"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{f} 必须是数字（秒）")
            if v < 0:
                raise ValueError(f"{f} 不能为负")
        elif f == "text" and kind == SEG_KEY:
            if not isinstance(v, str) or not v.strip():
                raise ValueError("text 不能为空")
            v = v.strip()
        else:
            v = "" if v is None else v
            if not isinstance(v, str):
                raise ValueError(f"{f} 必须是字符串")
        clean[f] = v
    if clean["end"] < clean["start"]:
        raise ValueError("end 不能早于 start")
    # optional rule provenance / color: presence-aware, so detach survives
    for f in _extra_fields(kind):
        v = _clean_extra(f, value.get(f), present=f in value)
        _put(clean, f, v)
    return clean


def merge_documents(base_doc, a_doc, b_doc, resolutions=None):
    """Analyze + build in one call."""
    changes, conflicts, stats = analyze(base_doc, a_doc, b_doc)
    doc, unresolved, errors = build_merged(changes, conflicts, resolutions)
    return {
        "document": doc,
        "changes": changes,
        "conflicts": conflicts,
        "stats": stats,
        "unresolved": unresolved,
        "errors": errors,
    }
