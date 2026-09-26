"""End-to-end API tests for the three-way merge save guards.

Persisting a merge (``saveAsProject`` or writing back to the base project)
must be all-or-nothing: whenever the adjudication leaves conflicts
unresolved or contains invalid custom values, ``POST /api/merge`` answers
**409** with the problem details, creates no project and never modifies the
base document. The plain preview still returns the conflicts and errors.

Run with: python3 test_merge_api.py  (or via pytest).
"""
import os
import tempfile

import pytest

os.environ.setdefault("STATIC_DIR", "/nonexistent")
_TMP = tempfile.mkdtemp(prefix="merge-api-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # entering the context triggers the startup event, which opens the SQLite DB
    with TestClient(app) as c:
        yield c

SEG = lambda sid, s, e, sp="", t="x": {
    "id": sid, "start": s, "end": e, "speaker": sp, "text": t}


def doc(segs=()):
    return {"segments": list(segs), "annotations": []}


def make_conflict_docs():
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    a = doc([SEG("s1", 0, 1, "Alice", "hello")])
    b = doc([SEG("s1", 0, 1, "Bob", "hello")])
    return base, a, b


# ---------- preview ----------

def test_preview_lists_unresolved_and_does_not_persist(client):
    base, a, b = make_conflict_docs()
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 200
    data = r.json()
    assert len(data["conflicts"]) == 1
    assert data["unresolved"] == [data["conflicts"][0]["id"]]
    assert data["errors"] == []
    assert "savedProject" not in data
    # conflicting item is omitted from the preview document
    assert data["document"]["segments"] == []


def test_preview_lists_validation_errors_for_invalid_custom(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": "", "end": ""}}},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["unresolved"] == []
    assert data["errors"] and data["errors"][0]["conflictId"] == cid
    # the invalid entry stays out of the preview document
    assert data["document"]["segments"] == []


# ---------- save as new project ----------

def test_save_as_with_unresolved_conflict_returns_409_and_creates_nothing(client):
    base, a, b = make_conflict_docs()
    before = client.get("/api/projects").json()
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b, "saveAsProject": "残缺项目"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"]
    assert isinstance(detail["errors"], list)
    # no project was created
    after = client.get("/api/projects").json()
    assert [p["id"] for p in after] == [p["id"] for p in before]
    assert all(p["name"] != "残缺项目" for p in after)


def test_save_as_with_invalid_custom_returns_409_and_creates_nothing(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": 5, "end": 1}}},
        "saveAsProject": "残缺项目",
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"] == []
    assert [e["conflictId"] for e in detail["errors"]] == [cid]
    assert all(p["name"] != "残缺项目"
               for p in client.get("/api/projects").json())


def test_save_as_with_valid_resolution_creates_project(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    r = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "b"}},
        "saveAsProject": "完整合并项目",
    })
    assert r.status_code == 200
    saved = r.json()["savedProject"]
    assert saved["name"] == "完整合并项目"
    project = client.get(f"/api/projects/{saved['id']}").json()
    assert project["document"]["segments"][0]["speaker"] == "Bob"


def test_fixing_invalid_resolution_then_save_succeeds(client):
    base, a, b = make_conflict_docs()
    cid = client.post("/api/merge", json={"base": base, "a": a, "b": b}) \
        .json()["conflicts"][0]["id"]
    bad = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom", "value": {"start": 9,
                                                            "end": 1}}},
        "saveAsProject": "修正后项目",
    })
    assert bad.status_code == 409
    good = client.post("/api/merge", json={
        "base": base, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom", "value": {
            "id": "s1", "start": 1, "end": 9,
            "speaker": "X", "text": "ok"}}},
        "saveAsProject": "修正后项目",
    })
    assert good.status_code == 200
    saved = good.json()["savedProject"]
    project = client.get(f"/api/projects/{saved['id']}").json()
    assert project["document"]["segments"][0]["end"] == 9


# ---------- write back to base project ----------

def test_write_back_with_unresolved_conflict_returns_409_and_keeps_base(client):
    created = db.create_project("基准项目", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    version_before = db.get_project(pid)["updatedAt"]

    r = client.post("/api/merge", json={"projectId": pid, "a": a, "b": b})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["unresolved"]
    # base document untouched
    fresh = db.get_project(pid)
    assert fresh["updatedAt"] == version_before
    assert fresh["document"]["segments"][0]["speaker"] == "A"


def test_direct_api_write_back_with_invalid_custom_is_refused(client):
    created = db.create_project("基准项目2", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    cid = client.post("/api/merge",
                      json={"projectId": pid, "a": a, "b": b})
    # the 409 response already names the unresolved conflict id
    cid = cid.json()["detail"]["unresolved"][0]
    before = db.get_project(pid)

    r = client.post("/api/merge", json={
        "projectId": pid, "a": a, "b": b,
        "resolutions": {cid: {"choice": "custom",
                              "value": {"start": "", "end": ""}}},
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["errors"] and detail["errors"][0]["conflictId"] == cid
    after = db.get_project(pid)
    assert after["updatedAt"] == before["updatedAt"]
    assert after["document"] == before["document"]


def test_write_back_with_valid_resolution_updates_base(client):
    created = db.create_project("基准项目3", doc([SEG("s1", 0, 1, "A", "hello")]))
    pid = created["id"]
    _, a, b = make_conflict_docs()
    cid = client.post("/api/merge",
                      json={"projectId": pid, "a": a, "b": b}) \
        .json()["detail"]["unresolved"][0]
    r = client.post("/api/merge", json={
        "projectId": pid, "a": a, "b": b,
        "resolutions": {cid: {"choice": "a"}},
    })
    assert r.status_code == 200
    assert r.json()["savedProject"]["id"] == pid
    assert db.get_project(pid)["document"]["segments"][0]["speaker"] == "Alice"


def _rule_ann(aid):
    return {
        "id": aid, "start": 3.0, "end": 9.0, "speaker": "受访者",
        "topic": "企业改制", "note": "base note",
        "color": "#5b8def", "source": "rule",
        "ruleId": "RULE", "ruleSlot": 0, "ruleName": "改制规则",
    }


def test_write_back_keeps_detach_and_color_then_rule_rerun_keeps_manual(client):
    # Base project holds a rule-generated annotation; the rule itself exists
    # and has been applied once.
    p = client.post("/api/projects", json={"name": "规则合并基准"}).json()
    pid = p["id"]
    base_doc = {
        "segments": [
            {"id": "s2", "start": 3, "end": 9, "speaker": "受访者",
             "text": "2003年改制，厂里变化大"},
        ],
        "annotations": [_rule_ann("an1")],
    }
    client.put(f"/api/projects/{pid}", json={
        "name": "规则合并基准", "document": base_doc,
        "documentVersion": p["documentVersion"]})
    rid = client.post(f"/api/projects/{pid}/rules", json={
        "name": "改制规则", "speaker": "受访者", "pattern": "改制",
        "matchType": "keyword", "caseSensitive": False, "minDuration": 2,
        "topic": "企业改制", "noteTemplate": "base note",
        "color": "#5b8def"}).json()["id"]
    # re-point the base annotation's ruleId at the real rule slot
    project = client.get(f"/api/projects/{pid}").json()
    base_doc = project["document"]
    base_doc["annotations"][0]["ruleId"] = rid
    client.put(f"/api/projects/{pid}", json={
        "name": "规则合并基准", "document": base_doc,
        "documentVersion": project["documentVersion"]})
    client.post(f"/api/rules/{rid}/apply")

    # A: manual recolor + detach; B: note only.
    a_doc = {"segments": base_doc["segments"],
             "annotations": [{**_rule_ann("an1"), "ruleId": rid,
                              "color": "#e36b6b"}]}
    a_doc["annotations"][0].pop("source")
    a_doc["annotations"][0].pop("ruleName")
    b_doc = {"segments": base_doc["segments"],
             "annotations": [{**_rule_ann("an1"), "ruleId": rid,
                              "note": "B 改的备注"}]}

    r = client.post("/api/merge", json={
        "projectId": pid, "a": a_doc, "b": b_doc})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["conflicts"] == [], data["conflicts"]
    # write-back succeeded without adjudication
    saved = client.get(f"/api/projects/{pid}").json()["document"]
    ann = saved["annotations"][0]
    assert ann["color"] == "#e36b6b"
    assert ann["note"] == "B 改的备注"
    assert "source" not in ann and "ruleName" not in ann
    assert ann["ruleId"] == rid and ann["ruleSlot"] == 0

    # Re-running the original rule must not overwrite the manual annotation.
    rerun = client.post(f"/api/rules/{rid}/apply").json()
    assert rerun["run"]["keptManualCount"] == 1
    ids = {a["id"]: a for a in rerun["document"]["annotations"]}
    assert ids["an1"]["color"] == "#e36b6b"
    assert "source" not in ids["an1"]
    assert any(a.get("source") == "rule" and a["id"] != "an1"
               for a in rerun["document"]["annotations"])


# ---------- invalid input ids -> 422, no merge result ----------

def test_duplicate_ids_in_a_copy_return_422_with_problem_details(client):
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    a = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    b = doc([SEG("s1", 0, 1, "B", "hello")])
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "未执行合并" in detail["message"]
    (p,) = detail["problems"]
    assert p["side"] == "a" and p["collection"] == "segments"
    assert p["reason"] == "duplicate" and p["id"] == "s1"
    assert p["indexes"] == [0, 1]
    # the message names side, collection, row and id
    assert "副本 A" in detail["message"]
    assert "segments" in detail["message"]
    assert "第 1、2 条" in detail["message"]
    assert "s1" in detail["message"]


def test_missing_id_returns_422_and_says_missing(client):
    base, _, b = make_conflict_docs()
    a = doc([{"start": 0, "end": 1, "speaker": "A", "text": "no id"}])
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 422
    detail = r.json()["detail"]
    (p,) = detail["problems"]
    assert p["reason"] == "missing" and p["id"] is None
    assert p["side"] == "a" and p["indexes"] == [0]
    assert "缺失" in detail["message"]


def test_duplicate_ids_in_base_document_return_422(client):
    base = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    _, a, b = make_conflict_docs()
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 422
    (p,) = r.json()["detail"]["problems"]
    assert p["side"] == "base" and p["reason"] == "duplicate"


def test_422_save_as_creates_no_project(client):
    before = client.get("/api/projects").json()
    a = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    r = client.post("/api/merge", json={
        "base": doc(), "a": a, "b": doc(), "saveAsProject": "不应创建"})
    assert r.status_code == 422
    after = client.get("/api/projects").json()
    assert [p["id"] for p in after] == [p["id"] for p in before]


def test_422_write_back_leaves_base_project_untouched(client):
    created = db.create_project("基准-无效输入", doc([SEG("s1", 0, 1, "A", "x")]))
    pid = created["id"]
    before = db.get_project(pid)
    a = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    r = client.post("/api/merge", json={
        "projectId": pid, "a": a, "b": doc([SEG("s1", 0, 1, "B", "y")])})
    assert r.status_code == 422
    after = db.get_project(pid)
    assert after["updatedAt"] == before["updatedAt"]
    assert after["document"] == before["document"]


def test_same_id_across_sides_and_kinds_is_valid_input(client):
    # identical ids across Base / A / B are how items are matched; a segment
    # and an annotation on the same side may also share an id
    base = {"segments": [SEG("s1", 0, 1, "A", "x")],
            "annotations": [{"id": "s1", "start": 0, "end": 1,
                             "speaker": "A", "topic": "", "note": ""}]}
    a = {"segments": [SEG("s1", 0, 1, "Alice", "x")],
         "annotations": [{"id": "s1", "start": 0, "end": 1,
                          "speaker": "A", "topic": "", "note": ""}]}
    b = {"segments": [SEG("s1", 0, 1, "A", "x")],
         "annotations": [{"id": "s1", "start": 0, "end": 1,
                          "speaker": "A", "topic": "", "note": ""}]}
    r = client.post("/api/merge", json={"base": base, "a": a, "b": b})
    assert r.status_code == 200
    data = r.json()
    assert data["conflicts"] == []
    assert data["document"]["segments"][0]["speaker"] == "Alice"


def test_fixing_the_file_allows_repreview(client):
    base = doc([SEG("s1", 0, 1, "A", "hello")])
    bad_a = doc([SEG("s1", 0, 1), SEG("s1", 1, 2)])
    b = doc([SEG("s1", 0, 1, "B", "hello")])
    bad = client.post("/api/merge", json={"base": base, "a": bad_a, "b": b})
    assert bad.status_code == 422
    # same request with the duplicate removed previews normally
    good_a = doc([SEG("s1", 0, 1), SEG("s2", 1, 2)])
    good = client.post("/api/merge",
                       json={"base": base, "a": good_a, "b": b})
    assert good.status_code == 200
    assert good.json()["stats"]["segments"]["added"] == 1
