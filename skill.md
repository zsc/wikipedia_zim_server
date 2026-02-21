---
name: kiwix-zim-server
description: Serve Wikipedia/ZIM files locally with search using kiwix-tools (kiwix-serve/kiwix-search), automate queries via the HTTP /search and /suggest endpoints, extract article HTML (infobox etc.), generate simple performance benchmarks/reports, and avoid common pitfalls (PATH, proxy env vars affecting localhost, Docker volume paths). Use when building, documenting, or debugging an offline Wikipedia ZIM “local searchable server”.
---

# Kiwix ZIM 本地可搜索服务：开发经验教训（Skill）

## 目标与边界

- 用 `kiwix-serve` 把 `.zim` 作为 **本地 HTTP 服务**提供浏览与搜索。
- 用 `kiwix-search` 作为 **无需 server 的 CLI 搜索**（适合单次/批处理，但要考虑启动开销）。
- 不追求“搜索结果页就带 infobox”：**infobox 属于条目页 HTML**，需要额外抓取条目内容。

## 最佳实践（按优先级）

### 1) 优先本机 `kiwix-serve`，Docker 只是兜底

- 先确保能找到二进制：`command -v kiwix-serve`、`kiwix-serve --version`。
- 若安装在 `~/.local/bin` 但找不到，修正 `PATH`（如 zsh 在 `~/.zshrc` 加 `export PATH="$HOME/.local/bin:$PATH"`）。
- 默认只监听 `127.0.0.1`；需要局域网访问才显式用 `ADDRESS=0.0.0.0`（安全优先）。

### 2) 识别“搜索结果”和“条目页”是两类返回

- **搜索结果**：`/search?...` 返回的是结果列表（标题 + snippet），不包含 infobox。
- **条目页**：`/content/<ZIMNAME>/<Title>` 返回条目 HTML，infobox 通常是 `<table class="infobox">...`。
- 需要 infobox/结构化信息时：用搜索拿到候选条目路径 → 再抓条目页 HTML。

### 3) 用 HTTP API 做脚本化，而不是“抓网页 + 解析 UI”

- `/search?pattern=...&content=<ZIMNAME>&format=xml` 适合机器读（标题/路径）。
- `/suggest?content=<ZIMNAME>&term=...&count=...` 适合联想/自动补全（JSON）。
- `ZIMNAME` 通常等于文件名去掉 `.zim`；也可以从任意条目 URL 里读出来（`/content/<ZIMNAME>/...`）。

### 4) 代理环境变量会坑 localhost：要显式 bypass

常见现象：你设置了 `http_proxy/https_proxy/all_proxy`（如 Clash/系统代理），`curl http://127.0.0.1:8080/...` 反而走代理导致超时/怪错误。

- `curl`：优先用 `--noproxy '*'`（最稳），或设置 `NO_PROXY=127.0.0.1,localhost`。
- Python：`requests` 默认会吃环境代理；做本地压测/抓取时优先用 `http.client` 或显式禁用代理。

### 5) 性能评估要区分“冷启动开销”和“单次查询延迟”

- `kiwix-search`：每次运行都会有 **进程启动 + 打开 ZIM + 初始化** 的固定成本；单次查询可能很快，但批量调用会被启动成本吞掉。
- `kiwix-serve`：一次启动后，多次查询基本是 **warm** 模式；适合交互/多次请求。
- 做 benchmark 时至少记录：
  - 启动时间（server 启动到可用；或 CLI 第一次调用耗时）
  - 搜索 p50/p95（多次重复、避免偶然值）
  - RSS/内存（不同 ZIM 差异很大）

## 推荐工作流（落地步骤）

1) 选定 ZIM（尽量不要 commit 大文件；用 symlink/外部路径挂载即可）。
2) 启动：`./serve`（优先本机 `kiwix-serve`；没有再用 Docker）。
3) 验证：
   - 浏览器打开首页能搜。
   - `/search` 返回 XML；`/suggest` 返回 JSON。
4) 需要 infobox 时：按“搜索 → 条目页抓取”两段式实现。
5) 要出报告时：用脚本自动跑多轮请求，生成 HTML 报告；把多个 ZIM 放到同一张对比表里。

## 反例（不要踩）

- 不要指望“搜索结果页”就保留 Wikipedia infobox：那不是同一类 endpoint。
- 不要在有系统代理时直接用 `curl http://127.0.0.1...` 去验证：先 `--noproxy '*'`。
- 不要默认对外网卡/0.0.0.0 监听：先本机闭环，再开放。

