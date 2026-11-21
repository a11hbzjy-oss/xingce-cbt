#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2 Production ETL + Difficulty Calibration + Bucket Sharding + CI Quality Gate
for ERRRC/xingcezhenti -> GitHub Pages.
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
Q_HEADER_RE = re.compile(r"^(#{2,4})\s*第\s*(\d{1,3})\s*题(?:\s*<sub>(.*?)</sub>)?\s*$")
MAT_HEADER_RE = re.compile(r"^#{2,4}\s*材料\s*([一二三四五六七八九十\d]+)\s*$")
OPT_LINE_RE = re.compile(r"^(?:[-*+]\s*)?(?:\*\*)?([A-D])(?:\*\*)?\s*[\.、．\)）:：]\s*(.*)$")
ANS_LINE_RE = re.compile(r"^(?:>\s*)?(?:\*\*)?(?:参考答案|正确答案|答案)(?:\*\*)?\s*[:：]\s*(?:\*\*)?([A-D])(?:\*\*)?\s*$", re.IGNORECASE)
EXP_HEADER_RE = re.compile(r"^(?:>\s*)?(?:\*\*)?(?:官方解析|试题解析|答案解析|名师解析|解析)(?:\*\*)?\s*[:：]?\s*(.*)$")
QID_RE = re.compile(r"qid\s*(\d+)", re.I)
IMG_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)
IMG_MD_RE = re.compile(r"!\[\[([^\]]+)\]\]|!\[([^\]]*)\]\(([^)]+)\)")
CHECKMARK_RE = re.compile(r"\s*[✅✔☑️]+\s*$")
DANGEROUS_HTML_RE = re.compile(r"<\s*(?:script|iframe|object|embed|style)\b[^>]*>[\s\S]*?<\s*/\s*(?:script|iframe|object|embed|style)\s*>", re.I)
EVENT_ATTR_RE = re.compile(r'\s+on[a-z]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)

# Source Tier Calibration (1 = closest to Judicial/Police Joint Exam style, 2 = National Enforcement/City, 3 = Provincial/Deputy-Provincial harder outliers)
TIER1_RE = re.compile(r"公安|司法|联考|甘肃|江西|湖北|湖南|广西|安徽|福建|河南|四川|陕西|云南|贵州|山西|河北|辽宁|吉林|黑龙江|内蒙古|宁夏|青海|新疆|海南|重庆|天津")
TIER2_RE = re.compile(r"行政执法|地市|国家|国考")
TIER3_RE = re.compile(r"副省|江苏|浙江|上海|广东|北京|山东")


def classify_source_tier(paper_name: str) -> int:
    if TIER1_RE.search(paper_name):
        return 1
    if TIER3_RE.search(paper_name):
        return 3
    if TIER2_RE.search(paper_name):
        return 2
    return 2


def extract_year(text: str) -> int:
    m = YEAR_RE.search(text)
    return int(m.group(1)) if m else 2024


def sanitize_html_str(text: str) -> str:
    if not text:
        return text
    text = DANGEROUS_HTML_RE.sub("", text)
    text = EVENT_ATTR_RE.sub("", text)
    return text


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
    text = sanitize_html_str(text)
    text = IMG_SRC_RE.sub(lambda m: f'src="{normalize_img_path(m.group(1), md_rel_path)}"', text)
    text = IMG_MD_RE.sub(lambda m: f"![{m.group(2) or '配图'}]({normalize_img_path(m.group(1) or m.group(3), md_rel_path)})", text)
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
        joined = stem + " " + " ".join(options)
        if "<img" in joined or "90-图片/" in joined or "填入问号处" in stem:
            return "图形推理"
        if re.search(r"是指|根据上述定义|下列属于.*的是|下列不属于.*的是", stem):
            return "定义判断"
        if len(stem) <= 45 and re.search(r"[:：∶]|对于.*相当于", stem):
            return "类比推理"
        return "逻辑判断"

    return MODULE_LABELS[mod_id]


def compute_raw_difficulty(q: dict, raw_tag: str) -> float:
    """
    Deterministic multi-factor difficulty proxy score (higher = harder)
    used to calibrate percentile-based 3-tier difficulty (1=基础, 2=中等, 3=偏难)
    within each (module, subType).
    """
    mod = q["module"]
    sub = q["subType"]
    stem = q["stem"]
    opts = q["options"]
    exp = q.get("explanation", "")
    tier = q.get("tier", 2)

    plain_stem = re.sub(r"<[^>]+>", "", stem)
    plain_exp = re.sub(r"<[^>]+>", "", exp)
    stem_len = len(plain_stem)
    exp_len = len(plain_exp)
    tex_cnt = len(re.findall(r'flag="tex"', stem + exp))

    # Base score from source tier (副省/江浙沪 slightly harder on average)
    score = {1: 0.0, 2: 0.25, 3: 0.55}.get(tier, 0.25)

    if mod in ("politics", "common"):
        if re.search(r"①.*②.*③", stem):
            score += 0.8
        if re.search(r"错误|不正确|不准确|不属于|说法有误", stem):
            score += 0.35
        score += min(1.0, (stem_len + sum(len(o) for o in opts)) / 320.0)
    elif mod == "verbal":
        if sub == "逻辑填空":
            blanks = max(1, len(re.findall(r"____|\s{4,}|（\s*）", plain_stem)))
            score += blanks * 0.65
            # Idiom density
            if all(len(re.sub(r"\s+", "", o)) >= 8 for o in opts):
                score += 0.55
            score += min(0.8, exp_len / 500.0)
        elif sub == "语句表达":
            if re.search(r"①.*②.*③.*④", stem):
                score += 0.9
            score += min(0.9, stem_len / 360.0)
        else:
            if re.search(r"细节|相符|不相符|未提及", stem + raw_tag):
                score += 0.5
            score += min(1.2, stem_len / 300.0) + min(0.6, exp_len / 500.0)
    elif mod == "quant":
        if re.search(r"排列组合|概率|几何|行程|最值|极值|函数", raw_tag + exp):
            score += 0.9
        elif re.search(r"方程|工程|利润|溶液|和差倍比|年龄", raw_tag + exp):
            score -= 0.3
        score += min(1.5, tex_cnt * 0.28) + min(1.0, exp_len / 350.0)
    elif mod == "reasoning":
        if sub == "图形推理":
            if re.search(r"立体|折叠|截面|三视图|六面体|拼合", raw_tag + stem + exp):
                score += 0.9
            elif re.search(r"数量|笔画|面数量|交点|对称", exp):
                score += 0.35
            score += min(0.8, exp_len / 320.0)
        elif sub == "定义判断":
            if re.search(r"不属于|不符合", stem):
                score += 0.45
            if len(re.findall(r"是指|定义为", stem)) >= 2:
                score += 0.6
            score += min(1.0, stem_len / 300.0)
        elif sub == "类比推理":
            if len(re.findall(r"[:：∶]", opts[0])) >= 2:
                score += 0.55
            score += min(0.8, exp_len / 300.0)
        else:
            if re.search(r"翻译|真假|组合|排列|前提|假设", raw_tag + exp):
                score += 0.6
            score += min(1.0, exp_len / 400.0) + min(0.6, stem_len / 280.0)
    elif mod == "data":
        if re.search(r"综合分析|能够推出|不能推出|两期|年均增长|拉动", raw_tag + stem + exp):
            score += 0.75
        score += min(1.4, tex_cnt * 0.25) + min(0.8, exp_len / 350.0)

    return round(score, 3)


def calibrate_difficulties(by_mod: dict):
    """
    Calibrate percentile-based difficulty (1=基础 30%, 2=中等 50%, 3=偏难 20%)
    within every (module, subType) for standalone questions, and per material group for Data Analysis.
    """
    for mod_id, arr in by_mod.items():
        if not arr:
            continue
        if mod_id == "data":
            groups = defaultdict(list)
            for q in arr:
                groups[q.get("groupId") or q["id"]].append(q)
            scored_groups = []
            for gid, grp in groups.items():
                avg_raw = sum(x.get("_rawDiff", 1.0) for x in grp) / len(grp)
                scored_groups.append((avg_raw, grp))
            scored_groups.sort(key=lambda x: x[0])
            n = len(scored_groups)
            for idx, (_, grp) in enumerate(scored_groups):
                pct = idx / max(1, n - 1) if n > 1 else 0.5
                d = 1 if pct < 0.30 else (2 if pct < 0.80 else 3)
                for q in grp:
                    q["diff"] = d
        else:
            buckets = defaultdict(list)
            for q in arr:
                buckets[q["subType"]].append(q)
            for st, sub_arr in buckets.items():
                sub_arr.sort(key=lambda x: x.get("_rawDiff", 1.0))
                n = len(sub_arr)
                for idx, q in enumerate(sub_arr):
                    pct = idx / max(1, n - 1) if n > 1 else 0.5
                    q["diff"] = 1 if pct < 0.30 else (2 if pct < 0.80 else 3)


def parse_repo_markdown(md_text: str, mod_id: str, rel_path: str) -> list:
    md_text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    paper_name = Path(rel_path).stem
    year = extract_year(paper_name)
    tier = classify_source_tier(paper_name)

    lines = md_text.split("\n")
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
    cur_q = None

    def flush_q():
        nonlocal cur_q, q_order_in_mat
        if not cur_q:
            return
        q_lines = cur_q["lines"]
        stem_lines, opts, exp_lines = [], {}, []
        cur_opt, ans_idx, in_exp = None, -1, False

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

            has_mat = bool(cur_mat_lines) and (mod_id == "data" or cur_q["level"] >= 3)
            # Hard exclusion: only Data Analysis uses shared materials; exclude 篇章阅读 & 一拖五
            if mod_id != "data":
                if has_mat or re.search(r"一拖五|篇章阅读|文章阅读", cur_q["raw_tag"]):
                    cur_q = None
                    return
                cn_len = len(re.findall(r"[\u4e00-\u9fff]", stem_clean))
                if mod_id == "verbal" and cn_len < 45 and re.search(r"原文|文中|上文|这篇文章|作者认为|文章|第[一二三四五六七八九十\d]+段", stem_clean):
                    cur_q = None
                    return
                if mod_id == "reasoning" and cn_len < 50 and re.search(r"根据上述材料|根据以上材料|根据所给材料|上述条件", stem_clean):
                    cur_q = None
                    return

            qid_str = cur_q["qid"] or hashlib.sha1((paper_name + ":" + str(cur_q["qnum"]) + ":" + stem_clean[:80]).encode("utf-8")).hexdigest()[:10]
            q_obj = {
                "id": f"q_{mod_id}_{qid_str}",
                "qid": cur_q["qid"],
                "module": mod_id,
                "subType": sub_type,
                "year": year,
                "tier": tier,
                "source": paper_name,
                "stem": stem_clean,
                "options": opts_clean,
                "answer": ans_idx,
                "explanation": exp_clean,
            }
            if mod_id == "data" and has_mat:
                mat_raw = "\n".join(cur_mat_lines).strip()
                if mat_raw:
                    q_order_in_mat += 1
                    q_obj["materialHtml"] = rewrite_images_in_text(mat_raw, rel_path)
                    q_obj["groupId"] = cur_mat_id
                    q_obj["groupOrder"] = q_order_in_mat
            q_obj["_rawDiff"] = compute_raw_difficulty(q_obj, cur_q["raw_tag"])
            questions.append(q_obj)
        cur_q = None

    for ln in lines[start_idx:]:
        s = ln.strip()
        mm = MAT_HEADER_RE.match(s)
        if mm:
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
            q_level = len(qm.group(1))
            if q_level <= 2 and mod_id != "data":
                cur_mat_lines = []
                cur_mat_id = ""
                q_order_in_mat = 0
            sub_info = qm.group(3) or ""
            qid_m = QID_RE.search(sub_info)
            qid = qid_m.group(1) if qid_m else ""
            raw_tag = sub_info.split("·")[-1].strip() if "·" in sub_info else sub_info.strip()
            cur_q = {"level": q_level, "qnum": int(qm.group(2)), "qid": qid, "raw_tag": raw_tag, "lines": []}
            continue
        if in_mat:
            cur_mat_lines.append(ln)
        elif cur_q is not None:
            cur_q["lines"].append(ln)

    flush_q()
    return questions


def deduplicate_by_module(questions: list, max_per_module: int) -> dict:
    by_mod = defaultdict(list)
    seen_qids, seen_fp = set(), set()

    non_data = [q for q in questions if q["module"] != "data"]
    # Prefer tier 1 (联考/执法) and newest years when deduplicating
    non_data.sort(key=lambda q: (q.get("tier", 2), -q.get("year", 0)))
    for q in non_data:
        if q.get("qid") and q["qid"] in seen_qids:
            continue
        img_sigs = "|".join(sorted(IMG_SRC_RE.findall(q["stem"] + "".join(q["options"]))))
        plain_sig = re.sub(r"[\s，。、；：！？\"'（）()【】]", "", re.sub(r"<[^>]+>", "", q["stem"][:160]) + "".join(q["options"]))
        fp = hashlib.sha1((plain_sig + "##" + img_sigs).encode("utf-8")).hexdigest()
        use_fp = bool(img_sigs) or len(re.findall(r"[\u4e00-\u9fff]", plain_sig)) >= 12
        if use_fp and fp in seen_fp:
            continue
        if q.get("qid"):
            seen_qids.add(q["qid"])
        if use_fp:
            seen_fp.add(fp)
        by_mod[q["module"]].append(q)

    groups = defaultdict(list)
    for q in questions:
        if q["module"] == "data" and q.get("groupId"):
            groups[q["groupId"]].append(q)
    sorted_groups = sorted(groups.values(), key=lambda g: (g[0].get("tier", 2), -max(x.get("year", 0) for x in g)))
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

    if max_per_module > 0:
        for mod_id, arr in list(by_mod.items()):
            if len(arr) <= max_per_module:
                continue
            if mod_id in ("verbal", "reasoning"):
                buckets = defaultdict(list)
                for q in arr:
                    buckets[q["subType"]].append(q)
                kept = []
                quota = max(150, max_per_module // max(1, len(buckets)))
                for st, bucket in buckets.items():
                    kept.extend(bucket[:quota])
                by_mod[mod_id] = kept[:max_per_module]
            elif mod_id == "data":
                kept, cur_g = [], None
                for q in arr:
                    if len(kept) >= max_per_module and q.get("groupId") != cur_g:
                        break
                    cur_g = q.get("groupId")
                    kept.append(q)
                by_mod[mod_id] = kept
            else:
                by_mod[mod_id] = arr[:max_per_module]
    return by_mod


def shard_into_buckets(mod_id: str, arr: list, bucket_size: int) -> list:
    """
    Split a module array into balanced ~bucket_size buckets so each bucket contains
    a representative mix of subtypes and difficulty levels (and intact Data Analysis groups).
    """
    if not arr:
        return []
    if mod_id == "data":
        groups = defaultdict(list)
        for q in arr:
            groups[q.get("groupId") or q["id"]].append(q)
        grp_list = list(groups.values())
        random.Random(2026).shuffle(grp_list)
        buckets, cur = [], []
        for g in grp_list:
            if cur and len(cur) + len(g) > bucket_size:
                buckets.append(cur)
                cur = []
            cur.extend(g)
        if cur:
            buckets.append(cur)
        return buckets

    # Interleave by (subType, diff) so every single bucket has balanced 3:5:2 difficulty & all subtypes!
    shuffled = list(arr)
    random.Random(2026).shuffle(shuffled)
    num_buckets = max(1, (len(shuffled) + bucket_size - 1) // bucket_size)
    buckets = [[] for _ in range(num_buckets)]
    # Sort by (subType, diff) then round-robin deal into buckets
    shuffled.sort(key=lambda q: (q["subType"], q.get("diff", 2)))
    for i, q in enumerate(shuffled):
        buckets[i % num_buckets].append(q)
    return [b for b in buckets if b]


def main():
    parser = argparse.ArgumentParser(description="V2 ETL: Difficulty Calibration + Bucket Sharding + CI Quality Gate")
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--out-dir", default="dist")
    parser.add_argument("--min-year", type=int, default=2020)
    parser.add_argument("--max-per-module", type=int, default=1200)
    parser.add_argument("--bucket-size", type=int, default=160)
    parser.add_argument("--min-total", type=int, default=500)
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
    calibrate_difficulties(by_mod)

    shards = []
    total = 0
    leak_count = 0
    print("\n[CI 质量与难度分布审计]")
    for mod_id in ("politics", "common", "verbal", "quant", "reasoning", "data"):
        arr = by_mod.get(mod_id, [])
        clean_arr = []
        for q in arr:
            for o in q["options"]:
                if "✅" in o:
                    leak_count += 1
            clean_arr.append({k: v for k, v in q.items() if k not in ("qid", "_rawDiff")})

        diff_cnt = Counter(q.get("diff", 2) for q in clean_arr)
        tier_cnt = Counter(q.get("tier", 2) for q in clean_arr)
        buckets = shard_into_buckets(mod_id, clean_arr, args.bucket_size)
        for bi, b in enumerate(buckets):
            fname = f"{mod_id}_{bi}.json"
            (data_dir / fname).write_text(json.dumps(b, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            shards.append({"module": mod_id, "file": fname, "count": len(b)})

        total += len(clean_arr)
        print(f"  - {MODULE_LABELS[mod_id]}: {len(clean_arr)} 题 ({len(buckets)} 桶) | 难度 基础:{diff_cnt[1]} 中等:{diff_cnt[2]} 偏难:{diff_cnt[3]} | 联考优先T1:{tier_cnt[1]}")
        if mod_id in ("verbal", "reasoning"):
            st_cnt = Counter(q["subType"] for q in clean_arr)
            print("      子题型分布:", " | ".join(f"{k}:{v}" for k, v in sorted(st_cnt.items())))

    # CI Quality Gate Assertions
    if leak_count > 0:
        sys.exit(f"[CI GATE FAIL] Detected {leak_count} unstripped ✅ answer leaks!")
    if total < args.min_total:
        sys.exit(f"[CI GATE FAIL] Only {total} questions parsed (< min-total {args.min_total}). Aborting deploy!")

    build_id = time.strftime("%Y%m%d%H%M%S")
    manifest = {"buildId": build_id, "totalQuestions": total, "shards": shards}
    (data_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Copy index.html into dist/ and swap runtime Tailwind CDN for pre-compiled static ./tailwind.css if present
    root_index = Path("index.html")
    if root_index.is_file():
        idx_html = root_index.read_text(encoding="utf-8")
        if (out_dir / "tailwind.css").is_file():
            idx_html = idx_html.replace(
                '<script src="https://cdn.tailwindcss.com/3.4.17"></script>',
                '<link rel="stylesheet" href="./tailwind.css" />',
            )
            print("[Build] Swapped runtime Tailwind CDN -> static minified ./tailwind.css")
        (out_dir / "index.html").write_text(idx_html, encoding="utf-8")

    print(f"\n[ETL] PASS! Build {build_id}, {total} real questions across {len(shards)} lazy buckets.")


if __name__ == "__main__":
    main()
