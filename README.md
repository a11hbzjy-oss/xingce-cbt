# 司法行政系统行测机考模拟系统（云端静态分片版）

零服务器、零本地脚本、完全免费。通过 GitHub Actions 在云端自动同步并清洗 [ERRRC/xingcezhenti](https://github.com/ERRRC/xingcezhenti) 历年真题，切分为按模块静态 JSON 分片后发布到 GitHub Pages——打开专属网址即可在电脑、平板或手机上直接模考。

## 三步上线（约 2 分钟）

1. **新建仓库并上传文件**
   - 在 GitHub 新建一个公开仓库（例如 `xingce-cbt`），将本压缩包解压后的全部文件（包含 `.github/workflows/deploy.yml`、`scripts/sync_bank.py`、`index.html`、`README.md`）推送或上传到仓库的 `main` 分支。
2. **开启 GitHub Pages**
   - 进入仓库的 **Settings → Pages**，将 **Build and deployment → Source** 切换为 **GitHub Actions**。
3. **等待自动构建并访问**
   - 切到仓库顶部的 **Actions** 页面，等待 `Build & Deploy Xingce CBT to GitHub Pages` 变绿（约 30–60 秒），点击页面显示的网址 `https://<你的用户名>.github.io/<仓库名>/` 即可直接开考。

> 之后每周会自动同步更新一次最新真题，也可以随时在 Actions 页面点击 **Run workflow** 手动触发更新。
