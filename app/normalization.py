"""Deterministic string-normalization engine.

A rule generation is an ordered list of steps.  The engine is pure: the same
(steps, raw string) pair always produces the same key and the same evidence.

Evidence is deliberately verbose: it preserves the original characters, shows
every intermediate string, and records the code points that changed.  Callers
store that evidence alongside immutable generation hashes.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

MAX_STEPS = 40
MAX_ALIAS_ENTRIES = 20000
MAX_REGEX_LENGTH = 240
MAX_EVIDENCE_TEXT = 400
MAX_EVIDENCE_SEGMENTS = 24
MAX_EVIDENCE_CHARS = 80

KNOWN_OPS = {
    "unicode_nfkc",
    "unicode_nfc",
    "unicode_nfd",
    "unicode_nfkd",
    "strip_diacritics",
    "trim",
    "collapse_ws",
    "remove_ws",
    "casefold",
    "lower",
    "upper",
    "strip_format",
    "strip_control",
    "strip_punct",
    "replace_map",
    "regex_replace",
}


class RuleError(ValueError):
    """A rule generation is structurally invalid."""


def unicode_escape(text: str) -> str:
    """ASCII-safe rendering that leaves ASCII printable characters alone."""
    return text.encode("unicode_escape").decode("ascii")


def codepoint_report(text: str, limit: int = MAX_EVIDENCE_CHARS) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ch in text[:limit]:
        code = ord(ch)
        if code < 128 and ch.isprintable():
            continue
        out.append(
            {
                "char": ch,
                "codepoint": f"U+{code:04X}",
                "name": unicodedata.name(ch, "UNKNOWN"),
                "category": unicodedata.category(ch),
            }
        )
    return out


def _clip(text: str, limit: int = MAX_EVIDENCE_TEXT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _changed_segments(before: str, after: str) -> tuple[list[dict[str, Any]], int]:
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    segments: list[dict[str, Any]] = []
    total = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        total += 1
        if len(segments) >= MAX_EVIDENCE_SEGMENTS:
            continue
        old = before[i1:i2]
        new = after[j1:j2]
        segments.append(
            {
                "before": old,
                "before_escaped": unicode_escape(old),
                "before_codepoints": codepoint_report(old),
                "after": new,
                "after_escaped": unicode_escape(new),
                "after_codepoints": codepoint_report(new),
            }
        )
    return segments, total


def _strip_punct(text: str, keep: str, also_symbols: bool) -> str:
    keep_set = set(keep)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in keep_set:
            out.append(ch)
        elif cat.startswith("P") or (also_symbols and cat.startswith("S")):
            continue
        else:
            out.append(ch)
    return "".join(out)


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise RuleError("steps 必须是数组")
    if not steps:
        raise RuleError("steps 不能为空")
    if len(steps) > MAX_STEPS:
        raise RuleError(f"规则步骤不能超过 {MAX_STEPS} 个")
    clean: list[dict[str, Any]] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RuleError(f"第 {idx + 1} 步必须是对象")
        op = step.get("op")
        if not isinstance(op, str) or op not in KNOWN_OPS:
            raise RuleError(f"第 {idx + 1} 步的 op 不受支持: {op!r}")
        out: dict[str, Any] = {"op": op}
        if op in {"strip_punct"}:
            keep = step.get("keep", "")
            if not isinstance(keep, str) or len(keep) > 64:
                raise RuleError(f"第 {idx + 1} 步 keep 必须是不超过 64 字符的字符串")
            also = step.get("also_symbols", False)
            if not isinstance(also, bool):
                raise RuleError(f"第 {idx + 1} 步 also_symbols 必须是布尔值")
            out["keep"] = keep
            if also:
                out["also_symbols"] = True
        elif op == "replace_map":
            mapping = step.get("map")
            if not isinstance(mapping, dict) or not mapping:
                raise RuleError(f"第 {idx + 1} 步 map 必须是非空对象")
            if len(mapping) > MAX_ALIAS_ENTRIES:
                raise RuleError(f"第 {idx + 1} 步别名条目不能超过 {MAX_ALIAS_ENTRIES}")
            norm_map: dict[str, str] = {}
            for key, value in mapping.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise RuleError(f"第 {idx + 1} 步 map 的键和值都必须是字符串")
                if key == "" or len(key) > 2000 or len(value) > 2000:
                    raise RuleError(f"第 {idx + 1} 步 map 条目的键不能为空且长度需 ≤ 2000")
                if key in norm_map and norm_map[key] != value:
                    raise RuleError(f"第 {idx + 1} 步 map 存在重复键 {key!r}")
                norm_map[key] = value
            out["map"] = dict(sorted(norm_map.items()))
        elif op == "regex_replace":
            pattern = step.get("pattern")
            replacement = step.get("replacement", "")
            flags = step.get("flags", "")
            if not isinstance(pattern, str) or not pattern:
                raise RuleError(f"第 {idx + 1} 步 pattern 必须是非空字符串")
            if len(pattern) > MAX_REGEX_LENGTH:
                raise RuleError(f"第 {idx + 1} 步正则长度不能超过 {MAX_REGEX_LENGTH}")
            if not isinstance(replacement, str) or not isinstance(flags, str):
                raise RuleError(f"第 {idx + 1} 步 replacement/flags 必须是字符串")
            if set(flags) - set("imsx"):
                raise RuleError(f"第 {idx + 1} 步 flags 只支持 i/m/s/x")
            if re.search(r"\((?:\.?[*+])\)[*+]", pattern):
                raise RuleError(f"第 {idx + 1} 步正则疑似存在灾难性回溯")
            try:
                compiled = re.compile(pattern, re.I if "i" in flags else 0)
            except re.error as exc:
                raise RuleError(f"第 {idx + 1} 步正则无效: {exc}") from exc
            try:
                compiled.sub(replacement, "probe")
            except (re.error, IndexError) as exc:
                raise RuleError(f"第 {idx + 1} 步 replacement 无效: {exc}") from exc
            out["pattern"] = pattern
            out["replacement"] = replacement
            if flags:
                out["flags"] = "".join(sorted(set(flags)))
        else:
            extra = set(step) - {"op", "note"}
            if extra:
                raise RuleError(f"第 {idx + 1} 步 ({op}) 不支持参数: {', '.join(sorted(extra))}")
            if "note" in step and not isinstance(step["note"], str):
                raise RuleError(f"第 {idx + 1} 步 note 必须是字符串")
        if step.get("note"):
            out["note"] = step["note"][:200]
        clean.append(out)
    return clean


def steps_hash(steps: list[dict[str, Any]]) -> str:
    """Hash the *behaviour* of a rule list.  Notes are documentation and are
    excluded so rewording a note never forces a rebuild or a new generation."""
    behavioural = [{k: v for k, v in step.items() if k != "note"} for step in steps]
    payload = json.dumps({"schema": 1, "steps": behavioural}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_one(text: str, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    op = step["op"]
    detail: dict[str, Any] = {}
    if op == "unicode_nfkc":
        result = unicodedata.normalize("NFKC", text)
    elif op == "unicode_nfc":
        result = unicodedata.normalize("NFC", text)
    elif op == "unicode_nfd":
        result = unicodedata.normalize("NFD", text)
    elif op == "unicode_nfkd":
        result = unicodedata.normalize("NFKD", text)
    elif op == "strip_diacritics":
        result = _strip_diacritics(text)
    elif op == "trim":
        result = text.strip()
    elif op == "collapse_ws":
        result = re.sub(r"\s+", " ", text)
    elif op == "remove_ws":
        result = re.sub(r"\s+", "", text)
    elif op == "casefold":
        result = text.casefold()
    elif op == "lower":
        result = text.lower()
    elif op == "upper":
        result = text.upper()
    elif op == "strip_format":
        result = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    elif op == "strip_control":
        result = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf"})
    elif op == "strip_punct":
        result = _strip_punct(text, step.get("keep", ""), bool(step.get("also_symbols")))
    elif op == "replace_map":
        mapping = step["map"]
        result = mapping.get(text, text)
        if result != text:
            detail["alias_used"] = {"from": text, "to": result}
    elif op == "regex_replace":
        flags = 0
        if "i" in step.get("flags", ""):
            flags |= re.I
        if "m" in step.get("flags", ""):
            flags |= re.M
        if "s" in step.get("flags", ""):
            flags |= re.S
        if "x" in step.get("flags", ""):
            flags |= re.X
        pattern = re.compile(step["pattern"], flags)
        result, count = pattern.subn(step.get("replacement", ""), text)
        detail["regex_substitutions"] = count
    else:  # pragma: no cover - validate_steps catches this
        raise RuleError(f"unknown op {op}")
    detail["changed"] = result != text
    detail["input_length"] = len(text)
    detail["output_length"] = len(result)
    return result, detail


def normalize(raw: str, steps: list[dict[str, Any]], *, include_text: bool = True) -> dict[str, Any]:
    """Normalize one raw string and return key + step-by-step evidence."""
    if not isinstance(raw, str):
        raise RuleError("raw 必须是字符串")
    current = raw
    records: list[dict[str, Any]] = []
    changed_steps = 0
    for idx, step in enumerate(steps):
        before = current
        current, detail = _apply_one(before, step)
        changed = current != before
        if changed:
            changed_steps += 1
        rec: dict[str, Any] = {
            "index": idx,
            "op": step["op"],
            "changed": changed,
            "input_length": len(before),
            "output_length": len(current),
        }
        if step.get("note"):
            rec["note"] = step["note"]
        if include_text:
            clipped_in, in_trunc = _clip(before)
            clipped_out, out_trunc = _clip(current)
            rec["input"] = clipped_in
            rec["output"] = clipped_out
            rec["input_escaped"] = unicode_escape(clipped_in)
            rec["output_escaped"] = unicode_escape(clipped_out)
            if in_trunc or out_trunc:
                rec["truncated"] = True
            if changed:
                segments, total = _changed_segments(before, current)
                rec["changes"] = segments
                rec["change_count"] = total
                rec["input_codepoints"] = codepoint_report(before)
                rec["output_codepoints"] = codepoint_report(current)
        if detail.get("alias_used"):
            rec["alias_used"] = detail["alias_used"]
        if "regex_substitutions" in detail:
            rec["regex_substitutions"] = detail["regex_substitutions"]
        records.append(rec)

    # The full raw string and full key are always retained: they are the mapping
    # basis that operators download and audit.  Only per-step display text is
    # clipped; the change segments and code-point lists are capped separately.
    return {
        "raw": raw,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "key": current,
        "empty": current == "",
        "status": "ok" if current else "empty_key",
        "changed_steps": changed_steps,
        "raw_escaped": unicode_escape(raw),
        "key_escaped": unicode_escape(current),
        "raw_codepoints": codepoint_report(raw),
        "key_codepoints": codepoint_report(current),
        "raw_truncated": False,
        "key_truncated": False,
        "raw_length": len(raw),
        "key_length": len(current),
        "steps": records,
    }


def normalize_many(values: Iterable[tuple[Any, str]], steps: list[dict[str, Any]], *, include_text: bool = True) -> list[dict[str, Any]]:
    out = []
    for item_id, raw in values:
        result = normalize(raw, steps, include_text=include_text)
        result["id"] = item_id
        out.append(result)
    return out


def smoke_test(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Validation probe set: catches empty-everything and invalid steps."""
    probes = [
        "  ACME  GmbH. ",
        "Müller GmbH",
        "Ｍｕｌｌｅｒ　ＧｍｂＨ",
        "华 为 技 术",
        "···",
    ]
    mappings = []
    for probe in probes:
        result = normalize(probe, steps, include_text=False)
        mappings.append({"raw": probe, "key": result["key"], "status": result["status"], "changed_steps": result["changed_steps"]})
    empty = sum(1 for m in mappings if m["status"] == "empty_key")
    warnings: list[str] = []
    if empty == len(mappings):
        raise RuleError("所有探针输入都归一化为空键；请检查规则")
    if empty:
        warnings.append(f"{empty} 个探针输入归一化为空，将进入冲突隔离区")
    keys = [m["key"] for m in mappings if m["key"]]
    if len(set(keys)) == 1 and len(keys) > 1:
        warnings.append("所有非空探针都映射到同一个键，请确认这是预期合并")
    return {"probe_mappings": mappings, "warnings": warnings}
