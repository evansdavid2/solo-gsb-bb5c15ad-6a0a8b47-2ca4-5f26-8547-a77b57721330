"""Pure unit tests for the rule engine (no FastAPI / DB required).

Run: python -m pytest backend/test_rules_engine.py -q
"""
from app import rules

RULE = {
    "name": "受访者谈改制", "speaker": "受访者", "pattern": "改制",
    "matchType": "keyword", "caseSensitive": False, "minDuration": 2,
    "topic": "企业改制",
    "noteTemplate": "{speaker}在{segments}个片段提到{keyword}",
    "color": "#4cae8f",
}
RID = "rule-abc"


def _document(*, annotations=()):
    return {
        "segments": [
            {"id": "s2", "start": 3, "end": 9, "speaker": "受访者",
             "text": "2003年改制，厂里变化大"},
        ],
        "annotations": list(annotations),
    }


def _owned_ann(ann_id="ann-rule-abc-0"):
    ann = rules.build_annotations(RULE, RID, _document()["segments"])[0]
    ann["id"] = ann_id
    return ann


def test_owned_annotation_is_updated_in_place():
    plan = rules.plan_apply(RULE, RID, _document(annotations=[_owned_ann()]))
    assert plan["stats"]["willUpdate"] == 1
    assert plan["stats"]["willCreate"] == 0
    assert plan["proposed"][0]["id"] == "ann-rule-abc-0"


def test_detached_by_id_collision_is_kept_manual():
    manual = _owned_ann()
    manual.pop("source")
    manual.pop("ruleName")
    manual["topic"] = "手工改过"
    plan = rules.plan_apply(RULE, RID, _document(annotations=[manual]))
    assert plan["stats"]["keptManual"] == 1
    assert plan["proposed"][0]["id"] != "ann-rule-abc-0"
    # the manual annotation is untouched in the input document
    assert manual["topic"] == "手工改过" and "source" not in manual


def test_detached_by_provenance_with_foreign_id_is_kept_manual():
    # The shape a three-way merge can produce: detached (no source), still
    # carrying the rule's ruleId/ruleSlot, but an id the rule would never
    # generate (e.g. a client id surviving the merge).
    manual = _owned_ann(ann_id="ann-client-1")
    manual.pop("source")
    manual.pop("ruleName")
    manual["color"] = "#e36b6b"
    plan = rules.plan_apply(RULE, RID, _document(annotations=[manual]))
    assert plan["stats"]["keptManual"] == 1
    assert plan["proposed"][0]["id"] != "ann-client-1"
    assert plan["proposed"][0]["source"] == "rule"
    assert manual["id"] == "ann-client-1" and manual["color"] == "#e36b6b"


def test_other_rules_and_plain_manual_annotations_untouched():
    other = _owned_ann(ann_id="ann-rule-xyz-0")
    other["ruleId"] = "rule-xyz"
    plain = {"id": "ann-manual", "start": 3, "end": 9, "speaker": "",
             "topic": "手工", "note": ""}
    plan = rules.plan_apply(
        RULE, RID, _document(annotations=[other, plain]))
    assert plan["stats"]["willCreate"] == 1
    assert plan["stats"]["keptManual"] == 0
    assert plan["proposed"][0]["id"] == "ann-rule-abc-0"


def test_tombstone_slot_is_suppressed_even_with_provenance_gone():
    # Manual deletion: no annotation survives at all for the slot.
    plan = rules.plan_apply(RULE, RID, _document(),
                            tombstone_slots={0})
    assert plan["stats"]["suppressed"] == 1
    assert plan["proposed"] == []
