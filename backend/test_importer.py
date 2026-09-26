"""Tests for transcript text parsing (SRT / WebVTT / legacy inline).

Run with: python3 test_importer.py  (or via pytest).
"""
import os
import shutil
import tempfile

import pytest

os.environ.setdefault("STATIC_DIR", "/nonexistent")
_TMP = tempfile.mkdtemp(prefix="importer-test-")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.importer import parse_document  # noqa: E402

SRT = """\
1
00:00:01,000 --> 00:00:04,000
访谈者: 感谢您今天接受访谈，
能先简单介绍一下自己吗？

2
00:00:04,500 --> 00:00:12,000
受访者: 好的。我叫李明，
在制造厂工作了二十多年。

3
00:00:12,000 --> 00:00:15,500
后来 2003 年改制
"""

# WebVTT: header, NOTE block, cue identifiers, cue settings, voice tag,
# styled span and a speaker-prefixed payload line.
VTT = """\
WEBVTT - 访谈转写

NOTE 这是一段说明
不应进入片段

00:00:01.000 --> 00:00:04.000
<v 访谈者>感谢您今天接受访谈</v>
能先介绍一下自己吗？

cue-2
00:00:05,000 --> 00:00:09,500 align:start position:0%
<c.highlight>我叫李明</c>，在制造厂工作

00:10.000 --> 00:13.000
受访者: 最早全靠手摇车床
"""

LEGACY = """\
[00:00:00] 访谈者: 感谢您今天接受访谈
[00:00:19] 受访者: 我叫李明，在制造厂工作了二十多年
[00:00:34] --> [00:00:40] 受访者: 后来 2003 年改制
[01:09.5] 访谈者: 能具体说说吗
00:00:03,500 --> 00:00:05,000  Speaker B: 行内时间仍可导入
[00:01.5] 大家好
"""


def test_srt_blocks():
    segs = parse_document(SRT)["segments"]
    assert len(segs) == 3
    s1, s2, s3 = segs
    assert (s1["start"], s1["end"]) == (1.0, 4.0)
    assert s1["speaker"] == "访谈者"
    assert s1["text"] == "感谢您今天接受访谈，\n能先简单介绍一下自己吗？"
    assert (s2["start"], s2["end"]) == (4.5, 12.0)
    assert s2["speaker"] == "受访者"
    assert s2["text"] == "好的。我叫李明，\n在制造厂工作了二十多年。"
    # payload without speaker prefix: speaker empty, body kept
    assert (s3["start"], s3["end"]) == (12.0, 15.5)
    assert s3["speaker"] == ""
    assert s3["text"] == "后来 2003 年改制"
    # explicit ids, stable order
    assert [s["id"] for s in segs]


def test_srt_without_blank_before_eof():
    raw = "1\n00:00:01,000 --> 00:00:02,000\n你好"
    segs = parse_document(raw)["segments"]
    assert len(segs) == 1
    assert segs[0]["text"] == "你好"
    assert (segs[0]["start"], segs[0]["end"]) == (1.0, 2.0)


def test_srt_cues_without_blank_separator():
    # tolerant of files missing the blank line between cues
    raw = ("1\n00:00:01,000 --> 00:00:02,000\n第一句\n"
           "2\n00:00:03,000 --> 00:00:04,000\n第二句")
    segs = parse_document(raw)["segments"]
    assert [s["text"] for s in segs] == ["第一句", "第二句"]


def test_vtt_header_and_cues():
    segs = parse_document(VTT)["segments"]
    assert len(segs) == 3
    s1, s2, s3 = segs
    # header + NOTE ignored, cue id "cue-2" ignored
    assert (s1["start"], s1["end"]) == (1.0, 4.0)
    assert s1["speaker"] == "访谈者"
    assert s1["text"] == "感谢您今天接受访谈\n能先介绍一下自己吗？"
    # cue settings consumed; <c> markup stripped, no voice speaker
    assert (s2["start"], s2["end"]) == (5.0, 9.5)
    assert s2["speaker"] == ""
    assert s2["text"] == "我叫李明，在制造厂工作"
    assert (s3["start"], s3["end"]) == (10.0, 13.0)
    assert s3["speaker"] == "受访者"
    assert s3["text"] == "最早全靠手摇车床"


def test_vtt_style_region_blocks_ignored():
    raw = (
        "WEBVTT\n\n"
        "STYLE\n::cue { color: lime }\n\n"
        "REGION\nid:side\nwidth:40%\n\n"
        "00:00:01.000 --> 00:00:02.000\n正文\n"
    )
    segs = parse_document(raw)["segments"]
    assert len(segs) == 1
    assert segs[0]["text"] == "正文"


def test_legacy_inline_timestamps_still_import():
    segs = parse_document(LEGACY)["segments"]
    assert len(segs) == 6
    assert segs[0] == {**segs[0], "start": 0.0, "end": 19.0,
                       "speaker": "访谈者", "text": "感谢您今天接受访谈"}
    assert segs[2]["start"] == 34.0 and segs[2]["end"] == 40.0
    # mm:ss(.mmm) form
    assert segs[3]["start"] == 69.5
    # inline "start --> end  Speaker: text" line must not become an
    # (empty) timing-only cue
    inline = segs[4]
    assert (inline["start"], inline["end"]) == (3.5, 5.0)
    assert inline["speaker"] == "Speaker B"
    assert inline["text"] == "行内时间仍可导入"
    # missing end time defaults to +1s for the last segment
    assert (segs[5]["start"], segs[5]["end"]) == (1.5, 2.5)
    assert segs[5]["speaker"] == "" and segs[5]["text"] == "大家好"


def test_mixed_blocks_and_inline_lines_keep_order():
    raw = (
        "00:00:01,000 --> 00:00:03,000\n字幕第一行\n字幕第二行\n\n"
        "[00:00:05] 访谈者: 行内片段\n"
    )
    segs = parse_document(raw)["segments"]
    assert [(round(s["start"], 3), s["text"]) for s in segs] == [
        (1.0, "字幕第一行\n字幕第二行"),
        (5.0, "行内片段"),
    ]


def test_vtt_header_without_blank_line():
    # tolerant of files with no blank between the WEBVTT signature and cue 1
    for raw in ("WEBVTT\n00:00:01.000 --> 00:00:02.000\n甲: 内容",
                "WEBVTT - 标题\n00:00:01.000 --> 00:00:02.000\n内容"):
        segs = parse_document(raw)["segments"]
        assert len(segs) == 1, raw
        assert (segs[0]["start"], segs[0]["end"]) == (1.0, 2.0)
    assert segs[0]["text"] == "内容"


def test_bom_prefixed_file():
    raw = "﻿1\n00:00:01,000 --> 00:00:02,500\n你好世界\n"
    segs = parse_document(raw)["segments"]
    assert len(segs) == 1 and segs[0]["text"] == "你好世界"


def test_empty_and_garbage_rejected():
    with pytest.raises(ValueError):
        parse_document("   ")
    with pytest.raises(ValueError):
        parse_document("没有任何\n时间戳的纯文本")


# ---------- HTTP upload (the exact path used by both UIs) ----------

DOC_WITH_NOTE = {
    "segments": [{"id": "old-1", "start": 0, "end": 1,
                  "speaker": "甲", "text": "旧转写"}],
    "annotations": [{"id": "ann-1", "start": 0, "end": 1,
                     "speaker": "甲", "topic": "必须保留的校注", "note": ""}],
}


def test_home_upload_srt_file_parses_blocks():
    with TestClient(app) as c:
        r = c.post("/api/import",
                   files={"file": ("访谈.srt", SRT.encode("utf-8"),
                                   "application/x-subrip")})
        assert r.status_code == 200, r.text
        segs = r.json()["segments"]
        assert len(segs) == 3
        assert segs[0]["start"] == 1.0 and segs[0]["end"] == 4.0
        assert segs[0]["speaker"] == "访谈者"
        assert segs[0]["text"] == "感谢您今天接受访谈，\n能先简单介绍一下自己吗？"


def test_home_upload_vtt_file_parses_cues():
    with TestClient(app) as c:
        r = c.post("/api/import",
                   files={"file": ("访谈.vtt", VTT.encode("utf-8"),
                                   "text/vtt")})
        assert r.status_code == 200, r.text
        segs = r.json()["segments"]
        assert [s["speaker"] for s in segs] == ["访谈者", "", "受访者"]
        assert segs[1]["text"] == "我叫李明，在制造厂工作"


def test_project_import_replaces_segments_keeps_annotations():
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"name": "导入保留校注"}).json()
        r = c.put(f"/api/projects/{p['id']}",
                  json={"name": "导入保留校注", "document": DOC_WITH_NOTE,
                        "documentVersion": p["documentVersion"]})
        assert r.status_code == 200, r.text

        r = c.post(f"/api/projects/{p['id']}/import",
                   files={"file": ("访谈.srt", SRT.encode("utf-8"),
                                   "application/x-subrip")})
        assert r.status_code == 200, r.text
        doc = r.json()["document"]
        assert len(doc["segments"]) == 3
        assert doc["segments"][0]["text"].startswith("感谢您今天接受访谈")
        assert all(s["id"] != "old-1" for s in doc["segments"])
        # annotations survive the re-import untouched
        assert doc["annotations"] == DOC_WITH_NOTE["annotations"]
        # and they are actually persisted
        saved = c.get(f"/api/projects/{p['id']}").json()["document"]
        assert saved["annotations"][0]["topic"] == "必须保留的校注"


def test_project_import_legacy_txt_still_works():
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"name": "行内时间"}).json()
        r = c.post(f"/api/projects/{p['id']}/import",
                   files={"file": ("t.txt", LEGACY.encode("utf-8"),
                                   "text/plain")})
        assert r.status_code == 200, r.text
        segs = r.json()["document"]["segments"]
        assert len(segs) == 6
        assert segs[4]["speaker"] == "Speaker B"
        assert segs[4]["text"] == "行内时间仍可导入"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
