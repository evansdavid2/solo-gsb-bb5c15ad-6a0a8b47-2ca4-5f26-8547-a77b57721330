"""Parse imported transcripts into the canonical document shape.

Accepted inputs:
1. canonical document JSON: {"segments": [...], "annotations": [...]}
2. JSON array of segment objects (start/end, speaker?, text)
3. subtitle text (``.srt`` / ``.vtt`` / ``.txt``), in either layout:

   * standard SRT / WebVTT blocks — an optional sequence number or cue
     identifier, a timing line and one or more payload lines::

         1
         00:00:01,000 --> 00:00:04,000
         访谈者: 感谢您今天接受访谈

         WEBVTT

         00:00:05.000 --> 00:00:09.000
         <v 受访者>我叫李明</v>
         在制造厂工作了二十多年

   * legacy inline timestamps, one segment per line::

         [00:00:01] Speaker A: 你好
         [00:01.5] 大家好
         00:00:03,500 --> 00:00:05,000  Speaker B: 文字
"""
from __future__ import annotations

import json
import re

from .db import new_id

# [hh:mm:ss(.|,)mmm] or [mm:ss(.|,)mmm]
_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?"
# legacy inline timestamps: "[00:01] text", "00:03,5 --> 00:05 text"
LINE_RE = re.compile(
    rf"^\s*\[?(?P<start>{_TS})\]?"
    rf"(?:\s*--?>\s*\[?(?P<end>{_TS})\]?)?"
    r"\s*(?P<rest>.*)$"
)
# a standalone timing line inside an SRT / WebVTT block
TIMING_RE = re.compile(
    rf"^\s*(?P<start>{_TS})\s*-->\s*(?P<end>{_TS})"
    r"(?:\s+(?P<settings>\S.*))?$"
)
SPEAKER_RE = re.compile(r"^(?P<speaker>[^:：]{1,40})[:：]\s*(?P<text>.+)$")
_NUM_RE = re.compile(r"^\d+$")
# WebVTT markup: <v 姓名>…</v>, timestamp/cue tags, class spans, comments
_VOICE_TAG_RE = re.compile(r"<\s*v(?:\.\w+)?\s+(?P<speaker>[^>]+)>",
                           re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
# WebVTT blocks that run until the next blank line (NOTE / STYLE / REGION).
# The WEBVTT signature line is handled separately: it only spans that one
# line, so files without a blank between header and first cue still parse.
_VTT_SKIP_RE = re.compile(
    r"^(?:NOTE|STYLE|REGION)(?:[\s:：]|$)", re.IGNORECASE)
_VTT_HEADER_RE = re.compile(r"^WEBVTT(?:[\s:：]|$)", re.IGNORECASE)
# VTT cue settings ("align:start position:50%"): colon-bearing tokens only,
# so a legacy inline line ("… --> …  Speaker B: 文字") is not read as one
_SETTINGS_RE = re.compile(r"^\s*\S+:\S+(?:\s+\S+:\S+)*\s*$")
_BOM = "\ufeff"


def _ts_to_seconds(token: str) -> float:
    token = token.replace(",", ".")
    parts = token.split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_document(raw: str | dict | list) -> dict:
    """Parse uploaded content into {"segments": [...], "annotations": [...]}."""
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        text = raw.lstrip(_BOM).strip()
        if not text:
            raise ValueError("导入内容为空")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _parse_text(text)

    if isinstance(data, dict):
        segments = data.get("segments")
        if not isinstance(segments, list):
            raise ValueError("JSON 文档必须包含 segments 数组")
        doc = {"segments": [_clean_segment(s) for s in segments],
               "annotations": []}
        annotations = data.get("annotations") or []
        if not isinstance(annotations, list):
            raise ValueError("annotations 必须是数组")
        doc["annotations"] = [_clean_annotation(a) for a in annotations]
        return doc

    if isinstance(data, list):
        segments, annotations = [], []
        for raw in data:
            seg = _clean_segment(raw)
            if raw.get("topic") or raw.get("note"):
                annotations.append({
                    "id": new_id("ann"),
                    "start": seg["start"], "end": seg["end"],
                    "speaker": seg["speaker"],
                    "topic": str(raw.get("topic", "") or ""),
                    "note": str(raw.get("note", "") or ""),
                })
            segments.append(seg)
        return {"segments": segments, "annotations": annotations}

    raise ValueError("无法识别的 JSON 结构")


def _clean_segment(raw: dict, fallback_id: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"片段必须是对象，收到 {type(raw).__name__}")
    start = _num(raw.get("start", raw.get("startTime", raw.get("begin"))), "start")
    end = _num(raw.get("end", raw.get("endTime", raw.get("stop"))), "end",
               default=start)
    if end < start:
        raise ValueError(f"片段 {start}-{end} 的结束时间早于开始时间")
    text = raw.get("text", raw.get("content", ""))
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    speaker = raw.get("speaker", raw.get("speakerName", "")) or ""
    sid = raw.get("id") or fallback_id or new_id("seg")
    if not isinstance(sid, str) or not sid:
        sid = new_id("seg")
    return {"id": sid, "start": start, "end": end,
            "speaker": str(speaker), "text": text}


def _clean_annotation(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("标注必须是对象")
    start = _num(raw.get("start", raw.get("startTime")), "start")
    end = _num(raw.get("end", raw.get("endTime")), "end", default=start)
    if end < start:
        raise ValueError(f"标注 {start}-{end} 的结束时间早于开始时间")
    return {
        "id": str(raw.get("id") or new_id("ann")),
        "start": start,
        "end": end,
        "speaker": str(raw.get("speaker", "") or ""),
        "topic": str(raw.get("topic", "") or ""),
        "note": str(raw.get("note", "") or ""),
    }


def _num(value, field: str, default: float | None = None) -> float:
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"缺少 {field} 时间")
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if ":" in value:
            return _ts_to_seconds(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 不是有效的时间: {value!r}")


def _strip_vtt_markup(line: str) -> str:
    """Turn ``<v 姓名>text</v>`` into ``姓名: text`` and drop other VTT tags.

    A voice tag donates its speaker as a ``speaker: text`` prefix, matching
    the inline convention used by the rest of the parser.
    """
    m = _VOICE_TAG_RE.search(line)
    if m:
        speaker = m.group("speaker").strip()
        line = line[:m.start()] + line[m.end():]
        line = _TAG_RE.sub("", line).strip()
        return f"{speaker}: {line}" if line and speaker else line
    return _TAG_RE.sub("", line).strip()


def _parse_payload(lines: list[str]) -> tuple[str, str]:
    """Split a cue's payload lines into (speaker, text).

    The speaker is only read from the *first* payload line
    (``名字: 文本``); subsequent lines are continuation text and are merged
    into the same segment in cue order.
    """
    text_lines = [_strip_vtt_markup(ln) for ln in lines]
    text_lines = [ln for ln in text_lines if ln != ""]
    if not text_lines:
        return "", ""
    speaker, first = "", text_lines[0]
    sm = SPEAKER_RE.match(first)
    if sm and not _NUM_RE.match(first):
        speaker, first = sm.group("speaker").strip(), sm.group("text").strip()
    body_lines = ([first] if first else []) + text_lines[1:]
    return speaker, "\n".join(body_lines)


def _parse_text(text: str) -> dict:
    """Parse SRT / WebVTT blocks and legacy inline-timestamp plain text.

    SRT / WebVTT cues are recognised by their timing line.  The preceding
    single line (sequence number or cue identifier) and the WebVTT header /
    NOTE / STYLE / REGION blocks are ignored; every non-empty line after the
    timing line belongs to the cue and is merged into one segment, keeping
    explicit start/end times and cue order.  Lines that are not part of a
    cue fall back to the legacy one-line-per-segment format.
    """
    segments: list[dict] = []
    pending: dict | None = None
    pending_start: float | None = None
    pending_locked = False

    # Cue scratch state:
    #   cue_lines   payload buffer (non-None while inside a cue)
    #   cue_start/end  the cue's explicit (locked) times
    #   id_candidate   a single non-timestamped line waiting to see whether
    #                  a timing line follows (SRT sequence number / VTT cue
    #                  id).  When one does, it is the next cue's id and is
    #                  discarded; when any other line follows, it belonged
    #                  to the open cue as ordinary payload.
    cue_lines: list[str] | None = None
    cue_start = cue_end = 0.0
    id_candidate: str | None = None
    skip_block = False

    def flush(next_start: float | None = None):
        nonlocal pending, pending_start, pending_locked
        if pending is not None:
            if not pending_locked:
                if next_start is not None and next_start > pending["start"]:
                    pending["end"] = next_start
                else:
                    pending["end"] = round(pending["start"] + 1.0, 3)
            segments.append(pending)
        pending, pending_start, pending_locked = None, None, False

    def use_id_candidate() -> None:
        """The buffered candidate is not a cue id: append it to its cue."""
        nonlocal id_candidate
        if id_candidate is not None:
            if cue_lines is not None:
                cue_lines.append(id_candidate)
            id_candidate = None

    def close_cue(next_start: float | None = None) -> None:
        """Flush any legacy pending line, then emit the finished cue.

        The cue is appended straight to ``segments`` (it carries explicit
        times), rather than parked in ``pending`` — otherwise the blank
        line separating SRT cues would immediately flush it away.  A
        buffered candidate is payload of the cue that just ended.
        """
        nonlocal cue_lines, id_candidate
        if cue_lines is None:
            return
        use_id_candidate()
        speaker, body = _parse_payload(cue_lines)
        flush(next_start)
        segments.append({
            "id": new_id("seg"),
            "start": cue_start,
            "end": cue_end,
            "speaker": speaker,
            "text": body,
        })
        cue_lines, id_candidate = None, None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            if skip_block:
                skip_block = False
                continue
            if cue_lines is not None:
                close_cue()
            else:
                id_candidate = None
                flush()
            continue

        if skip_block:
            continue

        if _VTT_HEADER_RE.match(line):
            # File signature line ("WEBVTT" / "WEBVTT - title"): drop just
            # this line, not everything up to the next blank.
            continue

        tm = TIMING_RE.match(line)
        if tm and (tm.group("settings") is None
                   or _SETTINGS_RE.match(tm.group("settings"))):
            # Timing line: the buffered candidate (if any) really was this
            # cue's sequence number / id and is dropped.  The line also
            # ends a previous cue even when the blank separator is missing.
            id_candidate = None
            if cue_lines is not None:
                close_cue()
            cue_start = _ts_to_seconds(tm.group("start"))
            cue_end = _ts_to_seconds(tm.group("end"))
            cue_lines = []
            continue

        if cue_lines is not None:
            use_id_candidate()
            # A single line immediately followed by a timing line is treated
            # as the next cue's sequence number / cue identifier.
            if _NUM_RE.match(line) or not LINE_RE.match(line):
                id_candidate = line
            else:
                cue_lines.append(line)
            continue

        if _VTT_SKIP_RE.match(line):
            skip_block = True
            continue

        m = LINE_RE.match(line)
        if not m:
            # Sequence number / cue identifier / stray text outside a cue:
            # ignored.  A following timing line would start a new cue anyway.
            continue

        start = _ts_to_seconds(m.group("start"))
        end = _ts_to_seconds(m.group("end")) if m.group("end") else None
        rest = m.group("rest").strip()

        speaker, body = "", rest
        sm = SPEAKER_RE.match(rest)
        if sm and not _NUM_RE.match(rest):
            speaker, body = sm.group("speaker").strip(), sm.group("text").strip()

        flush(start if pending_start is not None and start > pending_start
              else None)
        pending = {
            "id": new_id("seg"),
            "start": start,
            "end": start + 1.0,
            "speaker": speaker,
            "text": body,
        }
        pending_locked = False
        if end is not None:
            pending["end"] = end
            pending_locked = True
        pending_start = start

    if cue_lines is not None:
        close_cue()
    flush()

    if not segments:
        raise ValueError("未能从文本中解析出任何带时间戳的片段")
    return {"segments": segments, "annotations": []}
