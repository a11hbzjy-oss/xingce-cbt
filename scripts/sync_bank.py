#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Precision-calibrated ETL script for ERRRC/xingcezhenti -> GitHub Pages static JSON shards.
Exact format supported (verified against real repository Raw files):
  - YAML frontmatter header
  - ## 材料 1  (Data Analysis material blocks)
  - ## 第 1 题 <sub>qid 16070152 · 逻辑填空</sub>  (or ### 第 1 题 <sub>...</sub>)
  - - **A**. ... / - **B**. ... ✅  (automatically strips trailing ✅!)
  - **答案**：B
  - **官方解析**
  - <p>...</p> and <img src="../90-图片/..." /> (including flag="tex" inline formulas)
Uses Python 3 standard library only.
"""
import argparse
import hashlib
import json
import os
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

# Matches:
#   ## 第 1 题 <sub>qid 16070152 · 逻辑填空</sub>
#   ### 第 1 题 <sub>qid 16069826 · 平均数</sub>
#   ## 第1题
Q_HEADER_RE = re.compile(
    r"^#{2,4}\s*第\s*(\d{1,3})\s*题(?:\s*<sub>(.*?)</sub>)?\s*$"
)

# Matches: ## 材料 1  /  ## 材料一  /  ### 材料 2
MAT_HEADER_RE = re.compile(r"^#{2,4}\s*材料\s*([一二三四五六七八九十\d]+)\s*$")

# Matches: - **A**. 黔驴技穷  /  - **B**. 山穷水尽 ✅  /  A. xxx
OPT_LINE_RE = re.compile(
    r"^(?:[-*+]\s*)?(?:\*\*)?([A-D])(?:\*\*)?\s*[\.、．\)）:：]\s*(.*)$"
)

# Matches: **答案**：B  /  **答案**: C
ANS_LINE_RE = re.compile(
    r"^(?:>\s*)?(?:\*\*)?(?:参考答案|正确答案|答案)(?:\*\*)?\s*[:：]\s*(?:\*\*)?([A-D])(?:\*\*)?\s*$",
    re.IGNORECASE,
)

# Matches: **官方解析**  /  **解析**  /  **解析**：...
EXP_HEADER_RE = re.compile(
    r"^(?:>\s*)?(?:\*\*)?(?:官方解析|试题解析|答案解析|名师解析|解析)(?:\*\*)?\s*[:：]?\s*(.*)$"
)

QID_RE = re.compile(r"qid\s*(\d+)", re.I)
IMG_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)
IMG_MD_RE = re.compile(r"!\[\[([^\]]+)\]\]|!\[([^\]]*)\]\(([^)]+)\)")
CHECKMARK_RE = re.compile(r"\s*[✅✔☑️]+\s*$")


def extract_year(text: str) -> int:
    m = YEAR_RE.search(text)
    return int(m.group(1)) if m else 2024


def normalize_img_path(raw_target: str, md_rel_path: str) -> str:
    target = raw_target.strip().split("|")[0].split()[0].strip("<>\"'")
    if re.match(r"^(https?:|data:)", target, re.I):
        return target
    target = urllib.parse.unquote(target).replace("\\", "/")
    if "90-图片/" in target:
        return target[target.index("90-图片/") :]
    parts = md_rel_path.replace("\\", "/").split("/")
    md_dir = "/".join(parts[:-1]) if len(parts) > 1 else ""
    return os.path.normpath(os.path.join(md_dir, target)).replace("\\", "/").lstrip("./")


def rewrite_images_in_text(text: str, md_rel_path: str) -> str:
    if not text:
        return text
    # Rewrite HTML <img src="../90-图片/...">
    text = IMG_SRC_RE.sub(
        lambda m: f'src="{normalize_img_path(m.group(1), md_rel_path)}"',
        text,
    )
    # Rewrite Markdown ![](...)
    text = IMG_MD_RE.sub(
        lambda m: f"![{m.group(2) or '配图'}]({normalize_img_path(m.group(1) or m.group(3), md_rel_path)})",
        text,
    )
    return text


def map_subtype(mod_id: str, raw_tag: str, stem: str, options: list) -> str:
    tag = raw_tag.strip()
    if mod_id == "verbal":
        if re.search(r"逻辑填空|选词填空", tag):
            return "逻辑填空"
        if re.search(r"语句|排序|接语|下文|填空|衔接|病句|歧义", tag) and "逻辑填空" not in tag:
            return "语句表达"
        if re.search(r"片段阅读|中心理解|细节|标题|意图|词句理解|主旨|阅读", tag):
            return "阅读理解"
        # Stem fallback
        if re.search(r"填入画横线|依次填入|画横线部分最恰当|____|\s{4,}", stem):
            return "逻辑填空"
        if re.search(r"重新排列|排序|衔接最恰当|接下来最可能|填入横线处最恰当的一句", stem):
            return "语句表达"
        return "阅读理解"

    if mod_id == "reasoning":
        if "图形" in tag:
            return "图形推理"
        if "定义" in tag:
            return "定义判断"
        if "类比" in tag:
            return "类比推理"
        if re.search(r"逻辑|削弱|加强|翻译|真假|组合|排列|论证|推理", tag):
            return "逻辑判断"
        # Stem fallback
        joined = stem + " " + " ".join(options)
        if "<img" in joined or "90-图片/" in joined or "填入问号处" in stem:
            return "图形推理"
        if re.search(r"是指|根据上述定义|下列属于.*的是|下列不属于.*的是", stem):
            return "定义判断"
        if len(stem) <= 45 and re.search(r"[:：∶]|对于.*相当于", stem):
            return "类比推理"
        return "逻辑判断"

    return MODULE_LABELS[mod_id]


def parse_repo_markdown(md_text: str, mod_id: str, rel_path: str) -> list:
    md_text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    paper_name = Path(rel_path).stem
    year = extract_year(paper_name)

    lines = md_text.split("\n")
    # Strip YAML frontmatter if present (either --- delimited or key: "val" header lines before # Title)
    start_idx = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 30)):
            if lines[i].strip() == "---":
                start_idx = i + 1
                break
    else:
        for i in range(min(len(lines), 20)):
            if lines[i].startswith("# "):
                start_idx = i + 1
                break

    questions = []
    cur_mat_lines = []
    cur_mat_id = ""
    in_mat = False
    q_order_in_mat = 0

    cur_q = None  # dict holding lines, qnum, qid, raw_tag

    def flush_q():
        nonlocal cur_q, q_order_in_mat
        if not cur_q:
            return

        q_lines = cur_q["lines"]
        stem_lines = []
        opts = {}
        cur_opt = None
        ans_idx = -1
        in_exp = False
        exp_lines = []

        for ln in q_lines:
            s = ln.strip()
            if in_exp:
                exp_lines.append(ln)
                continue

            em = EXP_HEADER_RE.match(s)
            if em and (ans_idx >= 0 or len(opts) == 4):
                in_exp = True
                if em.group(1).strip():
                    exp_lines.append(em.group(1).strip())
                continue

            am = ANS_LINE_RE.match(s)
            if am:
                ans_idx = ord(am.group(1).upper()) - ord("A")
                cur_opt = None
                continue

            om = OPT_LINE_RE.match(s)
            if om and (cur_opt is not None or om.group(1).upper() == "A"):
                letter = om.group(1).upper()
                val = om.group(2).strip()
                # If this option carries ✅ and answer wasn't set yet, record it and strip ✅!
                if CHECKMARK_RE.search(val):
                    if ans_idx < 0:
                        ans_idx = ord(letter) - ord("A")
                    val = CHECKMARK_RE.sub("", val).strip()
                opts[letter] = val
                cur_opt = letter
                continue

            if cur_opt is not None:
                if s:
                    extra = CHECKMARK_RE.sub("", s).strip()
                    if CHECKMARK_RE.search(s) and ans_idx < 0:
                        ans_idx = ord(cur_opt) - ord("A")
                    opts[cur_opt] = (opts[cur_opt] + "\n" + extra).strip()
            else:
                stem_lines.append(ln)

        stem_raw = "\n".join(stem_lines).strip()
        ordered_opts = [CHECKMARK_RE.sub("", opts.get(c, "")).strip() for c in ("A", "B", "C", "D")]
        exp_raw = "\n".join(exp_lines).strip()

        if len(stem_raw) >= 4 and all(ordered_opts) and 0 <= ans_idx <= 3:
            stem_clean = rewrite_images_in_text(stem_raw, rel_path)
            opts_clean = [rewrite_images_in_text(o, rel_path) for o in ordered_opts]
            exp_clean = rewrite_images_in_text(exp_raw, rel_path) if exp_raw else "详见真题解析。"
            sub_type = map_subtype(mod_id, cur_q["raw_tag"], stem_clean, opts_clean)

            qid_str = cur_q["qid"] or hashlib.sha1(
                (paper_name + ":" + str(cur_q["qnum"]) + ":" + stem_clean[:80]).encode("utf-8")
            ).hexdigest()[:10]

            q_obj = {
                "id": f"q_{mod_id}_{qid_str}",
                "qid": cur_q["qid"],
                "module": mod_id,
                "subType": sub_type,
                "year": year,
                "source": paper_name,
                "stem": stem_clean,
                "options": opts_clean,
                "answer": ans_idx,
                "explanation": exp_clean,
            }

            if mod_id == "data" and cur_mat_lines:
                mat_raw = "\n".join(cur_mat_lines).strip()
                if mat_raw:
                    q_order_in_mat += 1
                    q_obj["material"] = rewrite_images_in_text(mat_raw, rel_path)
                    q_obj["groupId"] = cur_mat_id
                    q_obj["groupOrder"] = q_order_in_mat

            questions.append(q_obj)

        cur_q = None

    for ln in lines[start_idx:]:
        s = ln.strip()

        mm = MAT_HEADER_RE.match(s)
        if mod_id == "data" and mm:
            flush_q()
            in_mat = True
            cur_mat_lines = []
            q_order_in_mat = 0
            cur_mat_id = f"mat_{hashlib.sha1((paper_name + '_' + mm.group(1)).encode('utf-8')).hexdigest()[:8]}"
            continue

        qm = Q_HEADER_RE.match(s)
        if qm:
            flush_q()
            in_mat = False
            sub_info = qm.group(2) or ""
            qid_m = QID_RE.search(sub_info)
            qid = qid_m.group(1) if qid_m else ""
            raw_tag = sub_info.split("·")[-1].strip() if "·" in sub_info else sub_info.strip()
            cur_q = {
                "qnum": int(qm.group(1)),
                "qid": qid,
                "raw_tag": raw_tag,
                "lines": [],
            }
            continue

        if in_mat:
            cur_mat_lines.append(ln)
        elif cur_q is not None:
            cur_q["lines"].append(ln)

    flush_q()
    return questions


def deduplicate_by_module(questions: list, max_per_module: int) -> dict:
    by_mod = defaultdict(list)
    seen_qids = set()
    seen_fp = set()

    # Non-data modules: newest years first
    non_data = [q for q in questions if q["module"] != "data"]
    non_data.sort(key=lambda q: q.get("year", 0), reverse=True)
    for q in non_data:
        if q.get("qid") and q["qid"] in seen_qids:
            continue
        fp = hashlib.sha1(
            re.sub(r"[\s，。、；：！？\"'（）()【】<>/a-zA-Z0-9=_-]", "", q["stem"][:120] + "".join(q["options"])).encode("utf-8")
        ).hexdigest()
        # Only use text fingerprint when text has enough Chinese chars (skip image-only graphic reasoning)
        use_fp = len(re.findall(r"[\u4e00-\u9fff]", q["stem"])) >= 12
        if use_fp and fp in seen_fp:
            continue
        if q.get("qid"):
            seen_qids.add(q["qid"])
        if use_fp:
            seen_fp.add(fp)
        by_mod[q["module"]].append(q)

    # Data Analysis: keep intact 4-5 question material groups
    groups = defaultdict(list)
    for q in questions:
        if q["module"] == "data" and q.get("groupId"):
            groups[q["groupId"]].append(q)
    sorted_groups = sorted(groups.values(), key=lambda g: max(x.get("year", 0) for x in g), reverse=True)
    for grp in sorted_groups:
        if len(grp) < 4:
            continue
        grp.sort(key=lambda x: x.get("groupOrder", 0))
        first_qid = grp[0].get("qid", "")
        if first_qid and first_qid in seen_qids:
            continue
        if first_qid:
            seen_qids.add(first_qid)
        by_mod["data"].extend(grp[:5])

    # Cap per module with balanced subtypes
    if max_per_module > 0:
        for mod_id, arr in list(by_mod.items()):
            if len(arr) <= max_per_module:
                continue
            if mod_id in ("verbal", "reasoning"):
                buckets = defaultdict(list)
                for q in arr:
                    buckets[q["subType"]].append(q)
                kept = []
                quota = max(120, max_per_module // max(1, len(buckets)))
                for st, bucket in buckets.items():
                    kept.extend(bucket[:quota])
                by_mod[mod_id] = kept[:max_per_module]
            elif mod_id == "data":
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
    parser.add_argument("--min-total", type=int, default=100, help="Fail build if fewer than this many questions parsed")
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
        # Drop internal qid key to keep payload lean
        clean_arr = [{k: v for k, v in q.items() if k != "qid"} for q in arr]
        (data_dir / fname).write_text(json.dumps(clean_arr, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        shards.append({"module": mod_id, "file": fname, "count": len(clean_arr)})
        total += len(clean_arr)
        print(f"  - {MODULE_LABELS[mod_id]} ({fname}): {len(clean_arr)} questions")
        if mod_id in ("verbal", "reasoning"):
            sub_cnt = Counter(q["subType"] for q in clean_arr)
            for st, c in sorted(sub_cnt.items()):
                print(f"      · {st}: {c}")

    if total < args.min_total:
        sys.exit(f"[ERROR] Only parsed {total} questions (< min-total {args.min_total}). Aborting deploy!")

    build_id = time.strftime("%Y%m%d%H%M%S")
    manifest = {"buildId": build_id, "totalQuestions": total, "shards": shards}
    (data_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    root_index = Path("index.html")
    if root_index.is_file():
        shutil.copy2(root_index, out_dir / "index.html")
    print(f"[ETL] Success! Build {build_id}, total {total} deduplicated real questions in {out_dir}")


if __name__ == "__main__":
    main()
