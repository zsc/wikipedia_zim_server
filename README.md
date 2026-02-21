把 Wikipedia `.zim` 文件 serve 成本地可搜索的离线 Wiki（基于 `kiwix-serve`）。

## 快速开始（推荐：本机 `kiwix-serve`；没有就用 Docker）

在本目录运行：

```bash
./serve
```

`./serve` 会自动选择运行方式：

- 如果你的 `PATH` 里有 `kiwix-serve`（来自 kiwix-tools），就直接用本机二进制
- 否则，如果有 `docker`，就用 `ghcr.io/kiwix/kiwix-serve` 容器

然后打开：`http://127.0.0.1:8080/`（页面自带搜索框）。

查看本机 `kiwix-serve` 路径：

```bash
command -v kiwix-serve
```

如果你是把它装在 `~/.local/bin/kiwix-serve`，但 `command -v` 找不到，通常是 `PATH` 没包含 `~/.local/bin`。zsh 可在 `~/.zshrc` 里加：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

查看版本：

```bash
kiwix-serve --version
```

## 指定端口 / 指定 ZIM

```bash
PORT=9090 ./serve
ADDRESS=0.0.0.0 PORT=9090 ./serve   # 允许局域网访问（默认只监听 127.0.0.1）
./serve /path/to/your.zim
PORT=9090 ./serve /path/to/your.zim
```

## 搜索与返回结果示例

### 浏览器里搜

1. 打开 `http://127.0.0.1:8080/`
2. 在页面顶部搜索框输入关键词（例如：`earth` / `solar system` / `python`）
3. 回车后会看到“搜索结果列表 + snippets（命中片段）”，点进去就是对应条目页面

> 搜索“结果页”本身只是列表；Wikipedia 的 infobox、目录、图片等结构是在“条目页”里展示（离线 HTML 渲染出来的）。

### 用 HTTP API（脚本化）

先拿到 `ZIMNAME`：打开任意条目页，看浏览器地址栏形如
`http://127.0.0.1:8080/content/<ZIMNAME>/...`，其中 `<ZIMNAME>` 就是要用的值（通常等于文件名去掉 `.zim`）。

```bash
# 1) 全文搜索（返回 HTML 结果页）
curl --noproxy '*' 'http://127.0.0.1:8080/search?pattern=earth&content=<ZIMNAME>' | head

# 2) 全文搜索（返回 XML）
curl --noproxy '*' 'http://127.0.0.1:8080/search?pattern=solar&content=<ZIMNAME>&format=xml' | head

# 3) 搜索建议（JSON；该接口是 kiwix-serve 的内部接口，但前端也在用）
curl --noproxy '*' 'http://127.0.0.1:8080/suggest?content=<ZIMNAME>&term=ea&count=5'
```

本仓库默认这份 ZIM：`wikipedia_en_simple_all_maxi_2025-11.zim`，对应 `ZIMNAME=wikipedia_en_simple_all_maxi_2025-11`，下面命令可直接跑：

```bash
# 4) 全文搜索（XML），只看标题
curl --noproxy '*' -s 'http://127.0.0.1:8080/search?pattern=earth&content=wikipedia_en_simple_all_maxi_2025-11&format=xml&pageLength=5' \
  | grep -E '^    <title>|^      <title>'

# 5) 搜索建议（JSON），只看 value/path
curl --noproxy '*' -s 'http://127.0.0.1:8080/suggest?content=wikipedia_en_simple_all_maxi_2025-11&term=ear&count=3' \
  | grep -E '\"value\"|\"path\"'

# 6) 取词条页（HTML）并验证 infobox 存在（只取前 200KB 更快）
curl --noproxy '*' -s -H 'Range: bytes=0-200000' 'http://127.0.0.1:8080/content/wikipedia_en_simple_all_maxi_2025-11/Earth' \
  | grep -n '<table class=\"infobox\"' | head -n 1
```

另外，如果你只想要“本地搜索函数”风格（不需要 server），可以直接用 `kiwix-search`：

```bash
/usr/bin/time -p kiwix-search wikipedia_en_simple_all_maxi_2025-11.zim earth | head
```

## FAQ：infobox 等结构能保留吗？

- 能：条目内容来自 ZIM 里打包好的离线 HTML，infobox 通常会以 `<table class="infobox">...` 的形式正常显示。
- 但：搜索“结果列表页”不会把 infobox 嵌进去（只给标题 + snippets）；需要点进条目页查看完整结构。
- 另外：是否有图片取决于你下载的 ZIM 类型（`maxi` 往往包含图片；`nopic` 则没有）。

## 不用 Docker（本机安装 `kiwix-serve`）

只要你的系统里能找到 `kiwix-serve`（来自 kiwix-tools），`./serve` 会自动优先使用本机二进制。

## 备用：docker compose

```bash
cp .env.example .env
docker compose up
```

默认只在本机监听（`127.0.0.1`）。如果你要局域网访问，可以自行改 `docker-compose.yml` 里的 `ports:` 绑定地址。

如果你的 `.zim` 是指向别处的 symlink（本仓库就是），在 Docker 场景下建议把 `.env` 里的 `ZIM_PATH` 改成真实文件路径（比如 `../play_zim/...zim`），避免容器内找不到文件。
