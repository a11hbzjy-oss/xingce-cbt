#!/usr/bin/env bash
set -euo pipefail
mkdir -p dist
if command -v npx >/dev/null 2>&1; then
  npx --yes tailwindcss@3.4.17 -i scripts/tailwind.input.css -o dist/tailwind.css --content index.html --minify || true
fi
rm -rf /tmp/xingcezhenti
git clone --depth 1 --filter=blob:none --sparse https://github.com/ERRRC/xingcezhenti.git /tmp/xingcezhenti
(cd /tmp/xingcezhenti && git sparse-checkout set 01-政治理论 02-常识判断 03-言语理解与表达 04-数量关系 05-判断推理 06-资料分析)
python3 scripts/sync_bank.py --repo-dir /tmp/xingcezhenti --out-dir dist --min-year 2020 --max-per-module 1200 --bucket-size 160 --min-total 500
