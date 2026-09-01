"""OneBot V11 protocol helpers: message segment parsing / rendering.

Implements the subset of onebot-v11 needed by KiteChat:
- message segments: text, face, image, record, at, reply, forward/node
- CQ code <-> segment conversion (for legacy string messages)
- human-readable preview extraction
"""
from __future__ import annotations

import json
import re
from typing import Any

CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^,\]]*=[^,\]]*)*)\]")


def _parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    if not raw:
        return params
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip()] = _cq_unescape(v)
    return params


def _cq_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def _cq_unescape(s: str) -> str:
    return (
        s.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def text_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def text_unescape(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def cq_to_segments(message: str) -> list[dict]:
    """Parse a CQ-code string message into OneBot V11 segments."""
    segments: list[dict] = []
    pos = 0
    for m in CQ_RE.finditer(message):
        if m.start() > pos:
            segments.append(
                {"type": "text", "data": {"text": text_unescape(message[pos:m.start()])}}
            )
        segments.append({"type": m.group(1), "data": _parse_cq_params(m.group(2))})
        pos = m.end()
    if pos < len(message):
        segments.append(
            {"type": "text", "data": {"text": text_unescape(message[pos:])}}
        )
    return segments


def segments_to_cq(segments: list[dict]) -> str:
    out: list[str] = []
    for seg in segments:
        t = seg.get("type", "text")
        data = seg.get("data", {})
        if t == "text":
            out.append(text_escape(str(data.get("text", ""))))
        else:
            params = ",".join(f"{k}={_cq_escape(str(v))}" for k, v in data.items())
            out.append(f"[CQ:{t}" + (f",{params}" if params else "") + "]")
    return "".join(out)


def normalize_message(message: Any) -> list[dict]:
    """Accept str (CQ) or list of segments; return canonical segment list."""
    if message is None:
        return []
    if isinstance(message, str):
        return cq_to_segments(message)
    if isinstance(message, list):
        out = []
        for seg in message:
            if isinstance(seg, dict) and "type" in seg:
                out.append({"type": seg["type"], "data": seg.get("data", {}) or {}})
        return out
    return [{"type": "text", "data": {"text": str(message)}}]


def preview(segments: list[dict], limit: int = 60) -> str:
    """Short human-readable preview for session lists / notifications."""
    parts: list[str] = []
    for seg in segments:
        t = seg.get("type")
        data = seg.get("data", {})
        if t == "text":
            parts.append(str(data.get("text", "")))
        elif t == "image":
            parts.append("[图片]")
        elif t == "record":
            parts.append("[语音]")
        elif t == "face":
            parts.append(f"[表情{data.get('id', '')}]")
        elif t == "at":
            parts.append(f"@{data.get('name') or data.get('qq', '')}")
        elif t == "forward":
            parts.append("[合并转发]")
        elif t in ("node",):
            parts.append("[转发消息]")
        elif t == "reply":
            parts.append("[回复]")
        elif t == "video":
            parts.append("[视频]")
        elif t == "file":
            parts.append("[文件]")
        else:
            parts.append(f"[{t}]")
    text = "".join(parts).strip().replace("\n", " ")
    return text[:limit]


def classify(segments: list[dict]) -> str:
    """Primary message_type for storage/notifications."""
    if not segments:
        return "text"
    first = segments[0]["type"]
    if first in ("image", "record", "video", "file", "face", "forward"):
        # forward wins regardless of order
        if any(s["type"] == "forward" for s in segments):
            return "forward"
        return first
    if any(s["type"] == "forward" for s in segments):
        return "forward"
    return "text"


def extract_forward_nodes(segments: list[dict]) -> list[dict]:
    """Extract forward node payloads for display.

    Each node: {uin/name: sender, content: segments or CQ string, time}
    Supports nested forward content by normalizing recursively.
    """
    nodes: list[dict] = []
    for seg in segments:
        if seg.get("type") != "node":
            continue
        data = seg.get("data", {})
        content = data.get("content", [])
        if isinstance(content, str):
            content = cq_to_segments(content)
        elif isinstance(content, list):
            content = normalize_message(content)
        nodes.append(
            {
                "name": data.get("name") or data.get("nickname") or "未知用户",
                "uin": data.get("uin") or data.get("qq") or "",
                "time": data.get("time", 0),
                "content": content,
            }
        )
    return nodes


def build_forward_segments(title: str, brief: list[str],
                           nodes: list[dict]) -> list[dict]:
    """Build a OneBot V11 forward message from nodes."""
    return [
        {
            "type": "forward",
            "data": {
                "title": title,
                "brief": "\n".join(brief) if brief else title,
                "content": nodes,
            },
        }
    ]


def segments_json(segments: list[dict]) -> str:
    return json.dumps(segments, ensure_ascii=False)
