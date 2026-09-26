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
    """Connected components of the bipartite overlap graph."""
    adj_a = {i: set() for i in range(len(a_added))}
    adj_b = {j: set() for j in range(len(b_added))}
    for i, a in enumerate(a_added):
        for j, b in enumerate(b_added):
            if _overlap(a, b):
                adj_a[i].add(j)
                adj_b[j].add(i)

    seen_a, seen_b = set(), set()
    clusters = []
    for start_i in range(len(a_added)):
        if start_i in seen_a or not adj_a[start_i]:
            continue
        a_ids, b_ids = set(), set()
        stack = [("a", start_i)]
        while stack:
            side, idx = stack.pop()
            if side == "a":
                if idx in seen_a:
                    continue
                seen_a.add(idx)
                a_ids.add(idx)
                stack.extend(("b", j) for j in adj_a[idx])
            else:
                if idx in seen_b:
                    continue
                seen_b.add(idx)
                b_ids.add(idx)
                stack.extend(("a", i) for i in adj_b[idx])
        clusters.append((sorted(a_ids), sorted(b_ids)))
    return clusters


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
