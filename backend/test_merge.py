"""Tests for the three-way merge logic. Run: python -m pytest backend/"""
import copy

import pytest

from app.merge import (
    MergeInputError,
    analyze,
    build_merged,
    merge_documents,
    validate_documents,
)

SEG = lambda sid, s, e, sp="", t="x": {
    "id": sid, "start": s, "end": e, "speaker": sp, "text": t}
ANN = lambda aid, s, e, sp="", topic="", note="": {
    "id": aid, "start": s, "end": e, "speaker": sp, "topic": topic, "note": note}

# A rule-generated annotation as the rule engine writes it.
RULE_ANN = dict(ANN("n1", 0, 1, sp="A", topic="t", note="base note"),
                color="#5b8def", source="rule", ruleId="rule-x",
                ruleSlot=0, ruleName="规则X")


def detached(ann, **changes):
    """A manually edited rule annotation: ownership removed, provenance kept."""
    out = copy.deepcopy(ann)
    out.update(changes)
    out.pop("source", None)
    out.pop("ruleName", None)
    return out


def doc(segs=(), anns=()):
    return {"segments": list(segs), "annotations": list(anns)}


def test_unrelated_edits_merge_automatically():
    base = doc([SEG("s1", 0, 1, "A", "hello"), SEG("s2", 1, 2, "A", "world")])
    a = doc([SEG("s1", 0, 1, "Alice", "hello"), SEG("s2", 1, 2, "A", "world")])
    b = doc([SEG("s1", 0, 1, "A", "hello"), SEG("s2", 1, 2, "Bob", "world!!")])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    merged = {s["id"]: s for s in r["document"]["segments"]}
    assert merged["s1"]["speaker"] == "Alice"
    assert merged["s2"]["speaker"] == "Bob" and merged["s2"]["text"] == "world!!"
    assert r["stats"]["segments"]["modified"] == 2


def test_same_edit_is_not_a_conflict():
    base = doc([SEG("s1", 0, 1, "A", "x")])
    a = doc([SEG("s1", 0, 1, "Alice", "x")])
    r = merge_documents(base, a, copy.deepcopy(a))
    assert r["conflicts"] == []
    assert r["document"]["segments"][0]["speaker"] == "Alice"


def test_overlapping_field_edits_conflict_and_resolves_to_b():
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    a = doc([SEG("s1", 0, 1, "Alice", "hello")])
    b = doc([SEG("s1", 0, 1, "Bob", "hello")])
    changes, conflicts, stats = analyze(base, a, b)
    assert len(conflicts) == 1
    assert conflicts[0]["changedFields"] == ["speaker"]
    cid = conflicts[0]["id"]

    out, unresolved, errors = build_merged(changes, conflicts, {})
    assert unresolved == [cid]  # unresolved -> item omitted
    assert out["segments"] == []

    out, unresolved, errors = build_merged(
        changes, conflicts, {cid: {"choice": "b"}})
    assert unresolved == [] and errors == []
    assert out["segments"][0]["speaker"] == "Bob"


def test_field_level_non_overlap_within_same_item_merges():
    # A changes speaker, B changes text -> different fields -> auto-merge
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    a = doc([SEG("s1", 0, 1, "Alice", "hello")])
    b = doc([SEG("s1", 0, 1, "A", "你好")])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    s = r["document"]["segments"][0]
    assert s["speaker"] == "Alice" and s["text"] == "你好"


def test_modify_vs_delete_is_a_conflict():
    base = doc([SEG("s1", 0, 1, "A", "x")])
    a = doc([SEG("s1", 0, 2, "A", "x")])
    b = doc([])
    changes, conflicts, stats = analyze(base, a, b)
    assert len(conflicts) == 1
    assert conflicts[0]["modifyDelete"] is True
    cid = conflicts[0]["id"]

    # keep A's modification
    out, unresolved, _ = build_merged(changes, conflicts,
                                      {cid: {"choice": "a"}})
    assert out["segments"][0]["end"] == 2

    # honor B's deletion
    out, _, _ = build_merged(changes, conflicts, {cid: {"choice": "b"}})
    assert out["segments"] == []


def test_both_delete():
    base = doc([SEG("s1", 0, 1)])
    r = merge_documents(base, doc(), doc())
    assert r["document"]["segments"] == []
    assert r["conflicts"] == []
    assert r["stats"]["segments"]["deleted"] == 1


def test_independent_annotation_adds_no_overlap_both_kept():
    base = doc()
    a = doc(anns=[ANN("an1", 0, 1, topic="背景")])
    b = doc(anns=[ANN("an2", 5, 6, topic="结论")])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    ids = {x["id"] for x in r["document"]["annotations"]}
    assert ids == {"an1", "an2"}


def test_overlapping_independent_adds_form_interval_conflict():
    base = doc()
    a = doc(anns=[ANN("an1", 0, 10, topic="A的主题")])
    b = doc(anns=[ANN("an2", 5, 15, topic="B的主题")])
    changes, conflicts, stats = analyze(base, a, b)
    assert len(conflicts) == 1
    assert conflicts[0]["conflictType"] == "interval"
    cid = conflicts[0]["id"]

    # choose side A -> B's item removed
    out, _, _ = build_merged(changes, conflicts, {cid: {"choice": "a"}})
    assert [x["id"] for x in out["annotations"]] == ["an1"]

    # keep both
    out, _, _ = build_merged(changes, conflicts, {cid: {"choice": "keep"}})
    assert {x["id"] for x in out["annotations"]} == {"an1", "an2"}

    # drop both
    out, _, _ = build_merged(changes, conflicts, {cid: {"choice": "drop"}})
    assert out["annotations"] == []

    # custom merged value
    out, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom",
        "value": ANN("an3", 2, 12, topic="合并主题", note="折中")}})
    assert errors == []
    assert out["annotations"][0]["topic"] == "合并主题"


def test_touching_intervals_do_not_conflict():
    base = doc()
    a = doc(anns=[ANN("an1", 0, 10)])
    b = doc(anns=[ANN("an2", 10, 20)])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    assert len(r["document"]["annotations"]) == 2


def test_interval_cluster_chains_all_overlapping_items():
    base = doc()
    # an1 overlaps bn1, bn1 overlaps an2 -> one cluster of 4 items
    a = doc(anns=[ANN("an1", 0, 6), ANN("an2", 8, 12)])
    b = doc(anns=[ANN("bn1", 4, 9), ANN("bn2", 100, 101)])
    _, conflicts, _ = analyze(base, a, b)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert {x["id"] for x in c["aItems"]} == {"an1", "an2"}
    assert {x["id"] for x in c["bItems"]} == {"bn1"}


def test_partially_overlapping_fields_can_be_merged_per_field():
    # A changes speaker+note, B changes speaker+topic: speaker conflicts,
    # note and topic auto-merge; speaker adjudicated per field.
    base = doc(anns=[ANN("n1", 0, 1, "A", topic="t", note="base note")])
    a = doc(anns=[ANN("n1", 0, 1, "Alice", topic="t", note="A note")])
    b = doc(anns=[ANN("n1", 0, 1, "Bob", topic="new topic", note="base note")])
    changes, conflicts, _ = analyze(base, a, b)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["conflictingFields"] == ["speaker"]
    assert c["partialMerge"]["note"] == "A note"
    assert c["partialMerge"]["topic"] == "new topic"
    cid = c["id"]

    out, unresolved, errors = build_merged(changes, conflicts, {cid: {
        "choice": "fields", "fields": {"speaker": "b"}}})
    assert errors == [] and unresolved == []
    n = out["annotations"][0]
    assert n["speaker"] == "Bob"
    assert n["note"] == "A note"        # auto-merged from A
    assert n["topic"] == "new topic"    # auto-merged from B


def test_custom_resolution_validation():
    base = doc([SEG("s1", 0, 1, "A", "x")])
    a = doc([SEG("s1", 0, 1, "Alice", "x")])
    b = doc([SEG("s1", 0, 1, "Bob", "x")])
    changes, conflicts, _ = analyze(base, a, b)
    cid = conflicts[0]["id"]

    _, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom", "value": {"start": 5, "end": 1}}})
    assert errors  # end < start and missing fields
    _, unresolved, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom",
        "value": {"start": 2, "end": 3, "speaker": "X", "text": "ok"}}})
    assert errors == [] and unresolved == []


def test_deletion_and_addition_of_different_items():
    base = doc([SEG("s1", 0, 1), SEG("s2", 1, 2)])
    a = doc([SEG("s2", 1, 2), SEG("s3", 2, 3, t="new")])
    b = doc([SEG("s1", 0, 1), SEG("s2", 1, 2.5)])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    ids = {x["id"] for x in r["document"]["segments"]}
    assert ids == {"s2", "s3"}
    assert next(x for x in r["document"]["segments"] if x["id"] == "s2")["end"] == 2.5


def test_results_sorted_by_time():
    base = doc()
    a = doc(anns=[ANN("an1", 90, 100)])
    b = doc(anns=[ANN("an2", 0, 5), ANN("an3", 40, 50)])
    r = merge_documents(base, a, b)
    starts = [x["start"] for x in r["document"]["annotations"]]
    assert starts == sorted(starts)


# ---------- rule provenance / color extra fields ----------

def test_detach_and_recolor_merges_with_note_edit_on_other_side():
    # The bug this suite guards: A manually recolors a rule annotation and
    # detaches it, B only edits the note.  The merge must keep *both* edits
    # (and the detached state) without raising a conflict.
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN, color="#e36b6b")])
    b = doc(anns=[{**RULE_ANN, "note": "B note"}])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    m = r["document"]["annotations"][0]
    assert m["color"] == "#e36b6b"
    assert m["note"] == "B note"
    assert "source" not in m and "ruleName" not in m  # detachment survived
    assert m["ruleId"] == "rule-x" and m["ruleSlot"] == 0  # provenance kept


def test_extra_only_changes_are_detected_as_modifications():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[{**RULE_ANN, "color": "#e36b6b"}])
    r = merge_documents(base, a, copy.deepcopy(base))
    assert r["conflicts"] == []
    assert r["stats"]["annotations"]["modified"] == 1
    assert r["document"]["annotations"][0]["color"] == "#e36b6b"


def test_same_extra_field_changed_differently_is_a_conflict():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[{**RULE_ANN, "color": "#e36b6b"}])
    b = doc(anns=[{**RULE_ANN, "color": "#4cae8f"}])
    changes, conflicts, _ = analyze(base, a, b)
    assert len(conflicts) == 1
    assert conflicts[0]["conflictingFields"] == ["color"]
    cid = conflicts[0]["id"]

    # per-field adjudication picks B's color, everything else stays owned
    out, unresolved, errors = build_merged(changes, conflicts, {cid: {
        "choice": "fields", "fields": {"color": "b"}}})
    assert errors == [] and unresolved == []
    m = out["annotations"][0]
    assert m["color"] == "#4cae8f"
    assert m["source"] == "rule" and m["ruleId"] == "rule-x"


def test_detach_against_other_ownership_change_is_per_field_conflict():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN, note="A note")])
    b = doc(anns=[{**RULE_ANN, "source": "imported", "note": "B note"}])
    changes, conflicts, _ = analyze(base, a, b)
    assert [c["conflictingFields"] for c in conflicts] == [["note", "source"]]
    cid = conflicts[0]["id"]
    # detach (A) for source, B's note
    out, unresolved, errors = build_merged(changes, conflicts, {cid: {
        "choice": "fields",
        "fields": {"source": "a", "note": "b"}}})
    assert errors == [] and unresolved == []
    m = out["annotations"][0]
    assert "source" not in m and "ruleName" not in m
    assert m["note"] == "B note"
    assert m["ruleId"] == "rule-x" and m["ruleSlot"] == 0


def test_whole_item_side_choice_keeps_extras():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN, color="#e36b6b")])
    b = doc(anns=[{**RULE_ANN, "color": "#4cae8f"}])
    changes, conflicts, _ = analyze(base, a, b)
    cid = conflicts[0]["id"]
    out, _, errors = build_merged(changes, conflicts, {cid: {"choice": "a"}})
    assert errors == []
    m = out["annotations"][0]
    assert m["color"] == "#e36b6b"
    assert "source" not in m and m["ruleSlot"] == 0


def test_custom_resolution_preserves_detach_and_inherits_conflicting_color():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN, color="#e36b6b")])
    b = doc(anns=[{**RULE_ANN, "color": "#4cae8f"}])
    changes, conflicts, _ = analyze(base, a, b)
    cid = conflicts[0]["id"]
    custom = {"id": "n1", "start": 0, "end": 1, "speaker": "A",
              "topic": "t", "note": "custom"}
    out, _, errors = build_merged(changes, conflicts, {
        cid: {"choice": "custom", "value": custom}})
    assert errors == []
    m = out["annotations"][0]
    # conflicting color defaults to A; detach is not resurrected
    assert m["color"] == "#e36b6b"
    assert "source" not in m and "ruleName" not in m
    assert m["ruleId"] == "rule-x" and m["ruleSlot"] == 0
    assert m["note"] == "custom"


def test_custom_resolution_can_reattach_ownership_explicitly():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN)])
    b = doc(anns=[{**RULE_ANN, "color": "#4cae8f", "source": "imported"}])
    changes, conflicts, _ = analyze(base, a, b)
    assert conflicts and conflicts[0]["conflictingFields"] == ["source"]
    cid = conflicts[0]["id"]
    out, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom", "value": {
            **RULE_ANN, "color": "#123456", "note": "again"}}})
    assert errors == []
    m = out["annotations"][0]
    assert m["source"] == "rule" and m["ruleName"] == "规则X"
    assert m["color"] == "#123456"


def test_custom_extra_field_validation():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    changes, conflicts, _ = analyze(
        base,
        doc(anns=[{**RULE_ANN, "color": "#e36b6b"}]),
        doc(anns=[{**RULE_ANN, "color": "#4cae8f"}]))
    cid = conflicts[0]["id"]
    core = {"id": "n1", "start": 0, "end": 1, "speaker": "A",
            "topic": "t", "note": ""}
    _, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom", "value": {**core, "ruleSlot": "zero"}}})
    assert errors and "ruleSlot" in errors[0]["message"]
    # explicit null color normalizes to the default instead of failing
    out, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom", "value": {**core, "color": None}}})
    assert errors == []
    assert out["annotations"][0]["color"] == "#5b8def"


def test_add_add_extra_field_difference_is_a_conflict():
    a = doc(anns=[{**ANN("x", 0, 5), "color": "#111111"}])
    b = doc(anns=[{**ANN("x", 0, 5), "color": "#222222"}])
    changes, conflicts, _ = analyze(doc(), a, b)
    assert conflicts[0]["conflictingFields"] == ["color"]
    out, _, errors = build_merged(
        changes, conflicts,
        {conflicts[0]["id"]: {"choice": "b"}})
    assert errors == []
    assert out["annotations"][0]["color"] == "#222222"


def test_interval_custom_and_keep_preserve_extras():
    a = doc(anns=[{**ANN("ia", 0, 5, topic="t"),
                   "color": "#abcdef", "source": "rule",
                   "ruleId": "r1", "ruleSlot": 0, "ruleName": "R"}])
    b = doc(anns=[ANN("ib", 4, 9, topic="u")])
    changes, conflicts, _ = analyze(doc(), a, b)
    assert conflicts[0]["conflictType"] == "interval"
    cid = conflicts[0]["id"]

    out, _, errors = build_merged(changes, conflicts, {cid: {
        "choice": "custom",
        "value": {"id": "ic", "start": 0, "end": 9, "speaker": "",
                  "topic": "z", "note": ""}}})
    assert errors == []
    m = out["annotations"][0]
    assert m["color"] == "#abcdef" and m["source"] == "rule"

    out, _, errors = build_merged(changes, conflicts,
                                  {cid: {"choice": "keep"}})
    kept = {x["id"]: x for x in out["annotations"]}
    assert kept["ia"]["source"] == "rule" and kept["ia"]["ruleSlot"] == 0
    assert errors == []


def test_modify_delete_choice_keeps_survivor_extras():
    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    changes, conflicts, _ = analyze(
        base, doc(anns=[detached(RULE_ANN, color="#e36b6b")]), doc())
    assert conflicts[0]["modifyDelete"] is True
    out, _, errors = build_merged(
        changes, conflicts,
        {conflicts[0]["id"]: {"choice": "a"}})
    assert errors == []
    m = out["annotations"][0]
    assert m["color"] == "#e36b6b" and "source" not in m
    assert m["ruleId"] == "rule-x" and m["ruleSlot"] == 0


def test_detached_annotation_provenance_protects_on_rule_rerun():
    # End-to-end behavior promised by the provenance fields: after the merge
    # the detached annotation may carry an id the rule engine never
    # generated; re-running the rule must still treat it as manual content.
    from app import rules as rule_engine

    base = doc(anns=[copy.deepcopy(RULE_ANN)])
    a = doc(anns=[detached(RULE_ANN, color="#e36b6b")])
    b = doc(anns=[{**RULE_ANN, "note": "B note"}])
    merged = merge_documents(base, a, b)["document"]
    segments = [{"id": "s1", "start": 0, "end": 1, "speaker": "A",
                 "text": "x"}]
    rule = {"name": "规则X", "pattern": "", "matchType": "none",
            "topic": "t", "noteTemplate": "base note",
            "color": "#5b8def"}
    plan = rule_engine.plan_apply(
        rule, "rule-x", {"segments": segments,
                         "annotations": merged["annotations"]})
    assert plan["stats"]["keptManual"] == 1
    final = merged["annotations"] + plan["proposed"]
    manual = next(x for x in final if x["id"] == "n1")
    assert "source" not in manual and manual["color"] == "#e36b6b"
    assert plan["proposed"][0]["id"] != "n1"


# ---------- input id validation (before any index is built) ----------

def test_duplicate_segment_ids_within_one_side_are_rejected():
    base = doc([SEG("s1", 0, 1), SEG("s2", 1, 2), SEG("s1", 2, 3)])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(base, doc(), doc())
    (p,) = excinfo.value.problems
    assert p["side"] == "base" and p["collection"] == "segments"
    assert p["reason"] == "duplicate" and p["id"] == "s1"
    assert p["indexes"] == [0, 2]  # every row carrying the repeated id
    assert "基准" in str(excinfo.value) and "s1" in str(excinfo.value)


def test_duplicate_annotation_ids_on_side_b_report_all_rows():
    b = doc(anns=[ANN("n1", 0, 1), ANN("n2", 1, 2), ANN("n1", 2, 3),
                  ANN("n1", 3, 4)])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(doc(), doc(), b)
    (p,) = excinfo.value.problems
    assert p["side"] == "b" and p["collection"] == "annotations"
    assert p["reason"] == "duplicate"
    assert p["indexes"] == [0, 2, 3] and p["id"] == "n1"


def test_missing_id_is_reported_as_missing():
    a = doc([SEG("s1", 0, 1), {"start": 1, "end": 2, "text": "no id"}])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(doc(), a, doc())
    (p,) = excinfo.value.problems
    assert p["side"] == "a" and p["collection"] == "segments"
    assert p["reason"] == "missing" and p["id"] is None
    assert p["indexes"] == [1]
    assert "缺失" in str(excinfo.value)


def test_empty_blank_and_null_ids_count_as_missing():
    a = doc(anns=[ANN("", 0, 1), ANN("   ", 1, 2),
                  {"id": None, "start": 2, "end": 3}])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(doc(), a, doc())
    assert [p["reason"] for p in excinfo.value.problems] == ["missing"] * 3
    assert [p["indexes"] for p in excinfo.value.problems] == [[0], [1], [2]]


def test_non_string_id_is_invalid_not_missing():
    b = doc([SEG(7, 0, 1)])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(doc(), doc(), b)
    (p,) = excinfo.value.problems
    assert p["reason"] == "invalid" and p["id"] == 7
    assert p["indexes"] == [0]


def test_non_dict_item_counts_as_missing_id():
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(doc(["not-a-dict"]), doc(), doc())
    (p,) = excinfo.value.problems
    assert p["reason"] == "missing" and p["indexes"] == [0]


def test_problems_from_all_sides_and_collections_are_aggregated():
    base = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    a = doc(anns=[{"start": 0, "end": 1}])
    b = doc([SEG("s9", 0, 1), SEG("s9", 1, 2)])
    with pytest.raises(MergeInputError) as excinfo:
        merge_documents(base, a, b)
    keys = {(p["side"], p["collection"], p["reason"])
            for p in excinfo.value.problems}
    assert keys == {("base", "segments", "duplicate"),
                    ("a", "annotations", "missing"),
                    ("b", "segments", "duplicate")}


def test_same_id_across_sides_still_matches_for_comparison():
    # the normal case: identical ids across Base / A / B are how items are
    # matched — never a validation error
    base = doc([SEG("s1", 0, 1, "A", "x")], [ANN("n1", 0, 1)])
    a = doc([SEG("s1", 0, 1, "Alice", "x")], [ANN("n1", 0, 1, topic="t")])
    b = doc([SEG("s1", 0, 1, "A", "x")], [ANN("n1", 0, 1, topic="t")])
    r = merge_documents(base, a, b)
    assert r["conflicts"] == []
    assert r["document"]["segments"][0]["speaker"] == "Alice"
    assert r["document"]["annotations"][0]["topic"] == "t"


def test_segment_and_annotation_may_share_an_id_on_the_same_side():
    base = doc([SEG("x", 0, 1)], [ANN("x", 0, 1)])
    r = merge_documents(base, base, copy.deepcopy(base))
    assert r["conflicts"] == []
    assert r["document"]["segments"][0]["id"] == "x"
    assert r["document"]["annotations"][0]["id"] == "x"


def test_validate_documents_raises_nothing_for_valid_inputs():
    validate_documents(doc([SEG("s1", 0, 1)]), doc([SEG("s1", 0, 1)]),
                       doc([SEG("s2", 0, 1)]))
    validate_documents(None, None, None)  # empty docs are valid


def test_analyze_also_validates_before_indexing():
    with pytest.raises(MergeInputError):
        analyze(doc([SEG("s1", 0, 1), SEG("s1", 1, 2)]), doc(), doc())
