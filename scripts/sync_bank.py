#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloud ETL script for GitHub Actions:
Parses real civil service past-paper Markdown files from ERRRC/xingcezhenti,
deduplicates cross-province overlaps, preserves Data Analysis "一材多题" groups,
and emits static sharded JSON files into <out-dir>/data/ alongside index.html.
Uses Python 3 standard library only.
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

MODULE_FOLDERS = {
    "01-政治理论": "politics",
    "02-常识判断": "common",
    "03-言语理解与表达": "verbal",
    "04-数量关系": "quant",
    "05-判断推理": "reasoning",
    "06-资料分析": "data",
}

MODULE_LABELS = {
    "politics": "政治理论",
    "common": "常识判断",
    "verbal": "言语理解与表达",
    "quant": "数量关系",
    "reasoning": "判断推理",
    "data": "资料分析",
}

YEAR_RE = re.compile(r"(20[12]\d)")
HEADING_SUBTYPE_PATTERNS = [
    ("逻辑填空", re.compile(r"逻辑填空|选词填空")),
    ("语句表达", re.compile(r"语句表达|语句排序|语句衔接|下文推断")),
    ("阅读理解", re.compile(r"阅读理解|片段阅读|中心理解|细节判断|标题填入")),
    ("图形推理", re.compile(r"图形推理")),
    ("定义判断", re.compile(r"定义判断")),
    ("类比推理", re.compile(r"类比推理")),
    ("逻辑判断", re.compile(r"逻辑判断|演绎推理|论证")),
]

Q_START_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:\*\*)?(?:第\s*)?(\d{1,3})(?:\s*题)?[\.、．\)）](?:\*\*)?\s*(.*)$"
)
MAT_START_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:\*\*)?(?:【?\s*材料[一二三四五六七八九十\d]*\s*】?|资料[一二三四五六七八九十\d]*|根据以下资料|所给出的图、表、文字|根据下列资料|阅读以下材料|请根据下面)"
)
ANSWER_SECTION_RE = re.compile(
    r"^#{1,4}\s*(?:参考答案|答案与解析|答案及解析|试题解析|参考答案及解析)\s*$"
)
OPT_LINE_RE = re.compile(
    r"^(?:[-*+]\s*)?(?:\*\*)?([A-D])(?:\*\*)?\s*[\.、．\)）:：\s]\s*(.+)$"
)
ANS_INLINE_RE = re.compile(
    r"(?:^|\n)\s*(?:>\s*)?(?:\*\*)?(?:【\s*)?(?:参考答案|正确答案|答案)(?:\s*】)?(?:\*\*)?\s*[:：]?\s*(?:\*\*)?([A-D])(?:\*\*)?",
    re.IGNORECASE,
)
EXP_INLINE_RE = re.compile(
    r"(?:^|\n)\s*(?:>\s*)?(?:\*\*)?(?:【\s*)?(?:试题解析|答案解析|名师解析|解析|详解)(?:\s*】)?(?:\*\*)?\s*[:：]?\s*([\s\S]*)$"
)
IMG_MD_RE = re.compile(r"!\[\[([^\]]+)\]\]|!\[([^\]]*)\]\(([^)]+)\)")


def extract_year(filename: str) -> int:
    m = YEAR_RE.search(filename)
    return int(m.group(1)) if m else 2024


def infer_verbal_subtype(stem: str, heading_sub: str = "") -> str:
    if heading_sub in ("逻辑填空", "语句表达", "阅读理解"):
        return heading_sub
    if re.search(r"填入画横线|依次填入|画横线部分最恰当|____|（\s*）", stem):
        return "逻辑填空"
    if re.search(r"重新排列|排序|衔接最恰当|接下来最可能|填入横线处最恰当的一句|语病|歧义", stem):
        return "语句表达"
    return "阅读理解"


def infer_reasoning_subtype(stem: str, options: list, heading_sub: str = "") -> str:
    if heading_sub in ("图形推理", "定义判断", "类比推理", "逻辑判断"):
        return heading_sub
    joined = stem + " " + " ".join(options)
    if (
        IMG_MD_RE.search(joined)
        or re.search(r"从所给的四个选项中.*选择最合适的一个填入问号处|六个图形|立体图形|折叠|三视图|截面", stem)
    ):
        return "图形推理"
    if re.search(r"是指|定义为|根据上述定义|下列属于.*的是|下列不属于.*的是|符合上述定义", stem):
        return "定义判断"
    if (
        len(stem) <= 45
        and re.search(r"[:：∶]|对于.*相当于", stem)
        and all(len(o) <= 35 for o in options)
    ):
        return "类比推理"
    return "逻辑判断"


def resolve_rel_image_path(raw_target: str, md_rel_path: str) -> str:
    target = raw_target.strip().split("|")[0].split()[0].strip("<>\"'")
    if re.match(r"^(https?:|data:)", target, re.I):
        return target
    target = urllib.parse.unquote(target).replace("\\", "/")
    if "90-图片/" in target:
        return target[target.index("90-图片/") :]
    parts = md_rel_path.replace("\\", "/").split("/")
    md_dir = "/".join(parts[:-1]) if len(parts) > 1 else ""
    return os.path.normpath(os.path.join(md_dir, target)).replace("\\", "/").lstrip("./")


def parse_end_answer_section(text: str) -> dict:
    ans_map = {}
    blocks = re.split(r"(?m)^(?=(?:\*\*)?(?:第\s*)?\d{1,3}[\.、．\)）])", text.strip())
    for blk in blocks:
        blk = blk.strip()
        if not blk:
            continue
        m = re.match(r"^(?:\*\*)?(?:第\s*)?(\d{1,3})[\.、．\)）](?:\*\*)?\s*([\s\S]+)$", blk)
        if not m:
            continue
        qno = int(m.group(1))
        rest = m.group(2).strip()
        am = re.search(r"(?:答案[:：\s]*)?(?:\*\*)?([A-D])(?:\*\*)?", rest[:40], re.I)
        if not am:
            continue
        ans_idx = ord(am.group(1).upper()) - ord("A")
        em = EXP_INLINE_RE.search(rest)
        exp = em.group(1).strip() if em else rest[am.end() :].strip(" ：:。;；\n")
        ans_map[qno] = (ans_idx, exp)
    return ans_map


def parse_options_from_lines(lines: list) -> tuple:
    stem_lines = []
    opts = {}
    cur_letter = None
    for ln in lines:
        s = ln.strip()
        if not s:
            if cur_letter is None:
                stem_lines.append("")
            continue
        found_inline = list(re.finditer(r"(?:^|\s{2,}|\t)(?:\*\*)?([A-D])(?:\*\*)?\s*[\.、．\)）]\s*", s))
        if len(found_inline) >= 2 and found_inline[0].group(1) in ("A", "C"):
            for i, match in enumerate(found_inline):
                letter = match.group(1)
                start = match.end()
                end = found_inline[i + 1].start() if i + 1 < len(found_inline) else len(s)
                opts[letter] = s[start:end].strip()
                cur_letter = letter
            continue
        m = OPT_LINE_RE.match(s)
        if m:
            letter = m.group(1).upper()
            opts[letter] = m.group(2).strip()
            cur_letter = letter
            continue
        if cur_letter is not None:
            opts[cur_letter] = (opts[cur_letter] + "\n" + s).strip()
        else:
            stem_lines.append(ln)
    ordered_opts = [opts.get(c, "").strip() for c in ("A", "B", "C", "D")]
    return "\n".join(stem_lines).strip(), ordered_opts


def parse_repo_markdown(md_text: str, mod_id: str, rel_path: str) -> list:
    md_text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    paper_name = Path(rel_path).stem
    year = extract_year(paper_name)

    end_answers = {}
    body_lines = md_text.split("\n")
    for idx, ln in enumerate(body_lines):
        if ANSWER_SECTION_RE.match(ln.strip()):
            end_answers = parse_end_answer_section("\n".join(body_lines[idx + 1 :]))
            body_lines = body_lines[:idx]
            break

    questions = []
    cur_heading_sub = ""
    cur_material_lines = []
    cur_material_id = ""
    mat_counter = 0
    in_material = False
    cur_qno = None
    cur_q_lines = []
    q_order_in_mat = 0

    def norm_imgs(txt: str) -> str:
        def _repl(m):
            raw_p = m.group(1) or m.group(3)
            alt = m.group(2) or "配图"
            return f"![{alt}]({resolve_rel_image_path(raw_p, rel_path)})"
        return IMG_MD_RE.sub(_repl, txt)

    def flush_question():
        nonlocal cur_qno, cur_q_lines, q_order_in_mat
        if cur_qno is None or not cur_q_lines:
            cur_qno = None
            cur_q_lines = []
            return
        raw_block = "\n".join(cur_q_lines).strip()
        exp = ""
        ans_idx = -1
        exp_m = EXP_INLINE_RE.search(raw_block)
        if exp_m:
            exp = exp_m.group(1).strip()
            before_exp = raw_block[: exp_m.start()].strip()
        else:
            before_exp = raw_block
        ans_m = ANS_INLINE_RE.search(before_exp)
        if ans_m:
            ans_idx = ord(ans_m.group(1).upper()) - ord("A")
            qa_part = before_exp[: ans_m.start()].strip()
        else:
            qa_part = before_exp
        if ans_idx < 0 and cur_qno in end_answers:
            ans_idx, end_exp = end_answers[cur_qno]
            if not exp:
                exp = end_exp

        stem, options = parse_options_from_lines(qa_part.split("\n"))
        if len(stem) >= 4 and all(options) and 0 <= ans_idx <= 3:
            if mod_id == "verbal":
                sub_type = infer_verbal_subtype(stem, cur_heading_sub)
            elif mod_id == "reasoning":
                sub_type = infer_reasoning_subtype(stem, options, cur_heading_sub)
            else:
                sub_type = MODULE_LABELS[mod_id]

            mat_text = "\n".join(cur_material_lines).strip() if mod_id == "data" else ""
            if mod_id == "data" and mat_text:
                q_order_in_mat += 1
                grp_id, grp_ord = cur_material_id, q_order_in_mat
            else:
                grp_id, grp_ord = "", 0

            stem_clean = norm_imgs(stem)
            opts_clean = [norm_imgs(o) for o in options]
            fp_src = re.sub(r"\s+", "", stem_clean + "".join(opts_clean))[:200]
            qid = f"r_{mod_id}_{hashlib.sha1((paper_name + ':' + str(cur_qno) + ':' + fp_src).encode('utf-8')).hexdigest()[:10]}"
            q_obj = {
                "id": qid,
                "module": mod_id,
                "subType": sub_type,
                "year": year,
                "source": paper_name,
                "stem": stem_clean,
                "options": opts_clean,
                "answer": ans_idx,
                "explanation": norm_imgs(exp) if exp else "详见真题解析。",
            }
            if mat_text:
                q_obj["material"] = norm_imgs(mat_text)
                q_obj["groupId"] = grp_id
                q_obj["groupOrder"] = grp_ord
            questions.append(q_obj)
        cur_qno = None
        cur_q_lines = []

    for ln in body_lines:
        stripped = ln.strip()
        if stripped.startswith("#"):
            for sub_name, pat in HEADING_SUBTYPE_PATTERNS:
                if pat.search(stripped):
                    cur_heading_sub = sub_name
                    break
        if mod_id == "data" and MAT_START_RE.match(stripped):
            flush_question()
            mat_counter += 1
            cur_material_id = f"mat_{hashlib.sha1(f'{paper_name}_{mat_counter}'.encode('utf-8')).hexdigest()[:8]}"
            cur_material_lines = [] if stripped.startswith("#") else [ln]
            q_order_in_mat = 0
            in_material = True
            continue
        qm = Q_START_RE.match(stripped)
        if qm:
            qnum = int(qm.group(1))
            rest = qm.group(2).strip()
            if 1 <= qnum <= 160 and not (in_material and len(cur_material_lines) == 0 and qnum > 25):
                flush_question()
                in_material = False
                cur_qno = qnum
                cur_q_lines = [rest] if rest else []
                continue
        if in_material:
            cur_material_lines.append(ln)
        elif cur_qno is not None:
            cur_q_lines.append(ln)
        elif mod_id == "data" and stripped and not stripped.startswith("#"):
            if not cur_material_id:
                mat_counter += 1
                cur_material_id = f"mat_{hashlib.sha1(f'{paper_name}_{mat_counter}'.encode('utf-8')).hexdigest()[:8]}"
            cur_material_lines.append(ln)

    flush_question()
    return questions


def deduplicate_by_module(questions: list, max_per_module: int) -> dict:
    by_mod = defaultdict(list)
    seen_fp = set()

    # Non-data modules
    non_data = [q for q in questions if q["module"] != "data"]
    non_data.sort(key=lambda q: q.get("year", 0), reverse=True)
    for q in non_data:
        fp = hashlib.sha1(
            re.sub(r"[\s，。、；：！？\"'（）()【】]", "", q["stem"] + "".join(q["options"])).encode("utf-8")
        ).hexdigest()
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        by_mod[q["module"]].append(q)

    # Data Analysis: keep intact 4-5 question material groups
    groups = defaultdict(list)
    for q in questions:
        if q["module"] == "data":
            groups[q.get("groupId") or q["id"]].append(q)
    sorted_groups = sorted(groups.values(), key=lambda g: max(x.get("year", 0) for x in g), reverse=True)
    for grp in sorted_groups:
        if len(grp) < 4:
            continue
        grp.sort(key=lambda x: x.get("groupOrder", 0))
        mat_fp = hashlib.sha1(
            re.sub(r"\s+", "", grp[0].get("material", "")[:200] + grp[0]["stem"]).encode("utf-8")
        ).hexdigest()
        if mat_fp in seen_fp:
            continue
        seen_fp.add(mat_fp)
        by_mod["data"].extend(grp[:5])

    # Cap per module while keeping balanced subtypes if max_per_module > 0
    if max_per_module > 0:
        for mod_id, arr in list(by_mod.items()):
            if len(arr) <= max_per_module:
                continue
            if mod_id in ("verbal", "reasoning"):
                sub_buckets = defaultdict(list)
                for q in arr:
                    sub_buckets[q["subType"]].append(q)
                kept = []
                quota = max(80, max_per_module // max(1, len(sub_buckets)))
                for st, bucket in sub_buckets.items():
                    kept.extend(bucket[:quota])
                by_mod[mod_id] = kept[:max_per_module]
            elif mod_id == "data":
                # Keep whole groups up to max_per_module
                kept = []
                cur_g = None
                for q in arr:
                    if len(kept) >= max_per_module and q.get("groupId") != cur_g:
                        break
                    cur_g = q.get("groupId")
                    kept.append(q)
                by_mod[mod_id] = kept
            else:
                by_mod[mod_id] = arr[:max_per_module]
    return by_mod


def main():
    parser = argparse.ArgumentParser(description="Build static sharded question bank for GitHub Pages")
    parser.add_argument("--repo-dir", required=True, help="Local path to cloned ERRRC/xingcezhenti repo")
    parser.add_argument("--out-dir", default="dist", help="Output static site directory")
    parser.add_argument("--min-year", type=int, default=2020, help="Minimum exam year to include (default 2020)")
    parser.add_argument("--max-per-module", type=int, default=1200, help="Max deduplicated questions per module shard")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    all_questions = []
    file_count = 0
    for folder, mod_id in MODULE_FOLDERS.items():
        mod_path = repo_dir / folder
        if not mod_path.is_dir():
            continue
        for md_file in sorted(mod_path.rglob("*.md")):
            if md_file.name.lower() == "readme.md":
                continue
            if extract_year(md_file.stem) < args.min_year:
                continue
            file_count += 1
            rel_path = str(md_file.relative_to(repo_dir)).replace("\\", "/")
            text = md_file.read_text(encoding="utf-8", errors="replace")
            all_questions.extend(parse_repo_markdown(text, mod_id, rel_path))

    print(f"[ETL] Parsed {file_count} papers -> {len(all_questions)} raw questions")
    by_mod = deduplicate_by_module(all_questions, args.max_per_module)

    shards = []
    total = 0
    for mod_id in ("politics", "common", "verbal", "quant", "reasoning", "data"):
        arr = by_mod.get(mod_id, [])
        fname = f"{mod_id}.json"
        (data_dir / fname).write_text(json.dumps(arr, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        shards.append({"module": mod_id, "file": fname, "count": len(arr)})
        total += len(arr)
        print(f"  - {MODULE_LABELS[mod_id]} ({fname}): {len(arr)} questions")

    build_id = time.strftime("%Y%m%d%H%M%S")
    manifest = {"buildId": build_id, "totalQuestions": total, "shards": shards}
    (data_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Copy frontend index.html into dist/
    root_index = Path("index.html")
    if root_index.is_file():
        shutil.copy2(root_index, out_dir / "index.html")
    print(f"[ETL] Done! Build {build_id}, total {total} deduplicated real questions in {out_dir}")


if __name__ == "__main__":
    main()
