#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import math
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import http.client


@dataclass(frozen=True)
class HttpCase:
    name: str
    path: str
    headers: dict[str, str]
    read_limit: int | None = None


@dataclass(frozen=True)
class ZimResult:
    zim_path: str
    zim_size_gb: float
    filename_base: str
    content_root: str
    catalog_entry: dict[str, Any]
    query_pattern: str
    suggest_term: str
    article_label: str
    article_path: str
    server_cmd: list[str]
    listen_url: str
    ready_wall: float
    startup_s: float
    rss_kb: int | None
    http_results: list[dict[str, Any]]
    cli_first: dict[str, Any] | None
    cli_warm: dict[str, Any] | None


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except FileNotFoundError:
        return 127, ""


def _best_effort_sysctl(name: str) -> str | None:
    code, out = _run(["sysctl", "-n", name])
    if code != 0:
        return None
    return out.strip() or None


def _find_free_port(host: str, start_port: int = 8090, attempts: int = 200) -> int:
    for port in range(start_port, start_port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port found in [{start_port}, {start_port+attempts})")


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("empty values")
    idx = max(0, min(len(sorted_values) - 1, math.ceil(p * len(sorted_values)) - 1))
    return sorted_values[idx]


def _summarize_times(values: list[float]) -> dict[str, float]:
    s = sorted(values)
    return {
        "n": float(len(values)),
        "min": s[0],
        "p50": statistics.median(s),
        "mean": statistics.mean(s),
        "p95": _percentile(s, 0.95),
        "max": s[-1],
        "stdev": statistics.pstdev(s) if len(s) >= 2 else 0.0,
    }


def _http_get(host: str, port: int, path: str, *, headers: dict[str, str], read_limit: int | None) -> tuple[int, bytes, float]:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    start = time.perf_counter()
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    if read_limit is None:
        body = resp.read()
    else:
        body = resp.read(read_limit)
    elapsed = time.perf_counter() - start
    status = resp.status
    conn.close()
    return status, body, elapsed


def _wait_ready(host: str, port: int, timeout_s: float = 60.0) -> float:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            status, body, _ = _http_get(
                host,
                port,
                "/catalog/v2/entries?count=1",
                headers={"Connection": "close"},
                read_limit=64 * 1024,
            )
            if status == 200 and body.startswith(b"<?xml"):
                return time.time()
        except Exception as e:
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"server not ready within {timeout_s}s (last_err={last_err})")


def _parse_catalog_entry(xml_bytes: bytes) -> dict[str, Any]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    entry = root.find("a:entry", ns)
    if entry is None:
        return {}
    get_text = lambda tag: (entry.find(f"a:{tag}", ns).text or "").strip() if entry.find(f"a:{tag}", ns) is not None else ""
    content_href = ""
    for link in entry.findall("a:link", ns):
        href = (link.get("href") or "").strip()
        if href.startswith("/content/"):
            content_href = href
            break
    content_root = content_href[len("/content/") :] if content_href.startswith("/content/") else ""
    return {
        "title": get_text("title"),
        "name": get_text("name"),
        "flavour": get_text("flavour"),
        "category": get_text("category"),
        "language": get_text("language"),
        "articleCount": get_text("articleCount"),
        "mediaCount": get_text("mediaCount"),
        "tags": get_text("tags"),
        "updated": get_text("updated"),
        "content_href": content_href,
        "content_root": content_root,
    }


def _bench_http(host: str, port: int, case: HttpCase, runs: int, warmups: int) -> dict[str, Any]:
    # Warmups (ignored)
    for _ in range(warmups):
        _http_get(host, port, case.path, headers=case.headers, read_limit=case.read_limit)

    times: list[float] = []
    sizes: list[int] = []
    statuses: dict[int, int] = {}
    for _ in range(runs):
        status, body, elapsed = _http_get(host, port, case.path, headers=case.headers, read_limit=case.read_limit)
        times.append(elapsed)
        sizes.append(len(body))
        statuses[status] = statuses.get(status, 0) + 1

    stats = _summarize_times(times)
    return {
        "name": case.name,
        "path": case.path,
        "runs": runs,
        "warmups": warmups,
        "status_counts": statuses,
        "bytes_mean": statistics.mean(sizes) if sizes else 0,
        "bytes_min": min(sizes) if sizes else 0,
        "bytes_max": max(sizes) if sizes else 0,
        "times": stats,
    }


def _bench_cli(cmd: list[str], runs: int) -> dict[str, Any]:
    times: list[float] = []
    rc_counts: dict[int, int] = {}
    for _ in range(runs):
        start = time.perf_counter()
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        rc_counts[p.returncode] = rc_counts.get(p.returncode, 0) + 1
    return {"cmd": cmd, "runs": runs, "return_codes": rc_counts, "times": _summarize_times(times)}


def _bench_cli_once(cmd: list[str]) -> dict[str, Any]:
    start = time.perf_counter()
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elapsed = time.perf_counter() - start
    return {"cmd": cmd, "return_code": p.returncode, "time_s": elapsed}


def _html_escape(s: str) -> str:
    return html.escape(s, quote=True)


def _fmt_s(v: float) -> str:
    if v < 1:
        return f"{v*1000:.1f} ms"
    return f"{v:.3f} s"


def _first_case_p50(zim: "ZimResult", prefix: str) -> float | None:
    for h in zim.http_results:
        name = str(h.get("name", ""))
        if name.startswith(prefix):
            try:
                return float(h["times"]["p50"])
            except Exception:
                return None
    return None


def _ratio_slow_text(a: float, b: float) -> str:
    if a <= 0 or b <= 0:
        return ""
    slow = max(a, b)
    fast = min(a, b)
    return f"{slow/fast:.1f}×"


def _resolve_path(repo_dir: str, p: str) -> str:
    if not os.path.isabs(p):
        p = os.path.join(repo_dir, p)
    return os.path.realpath(p)


def _guess_queries(language: str, filename_base: str) -> tuple[str, str, str]:
    lang = (language or "").lower()
    base = (filename_base or "").lower()
    if any(x in lang for x in ("zho", "chi")) or "_zh_" in base or base.startswith("wikipedia_zh_"):
        return ("地球", "地", "地球")
    return ("earth", "ear", "Earth")


def _pick_article_path(
    host: str,
    port: int,
    *,
    content_root: str,
    preferred_title: str,
    search_pattern: str,
) -> tuple[str, str]:
    # Try preferred title first.
    preferred_path = f"/content/{urllib.parse.quote(content_root)}/{urllib.parse.quote(preferred_title)}"
    try:
        status, _, _ = _http_get(
            host,
            port,
            preferred_path,
            headers={"Connection": "close", "Range": "bytes=0-200000"},
            read_limit=16 * 1024,
        )
        if status in (200, 206):
            return preferred_title, preferred_path
    except Exception:
        pass

    # Fallback: take first search result link (XML).
    q = urllib.parse.urlencode(
        {"pattern": search_pattern, "content": content_root, "format": "xml", "pageLength": "1"}
    )
    status, xml_body, _ = _http_get(host, port, f"/search?{q}", headers={"Connection": "close"}, read_limit=256 * 1024)
    if status != 200:
        return preferred_title, preferred_path
    try:
        root = ET.fromstring(xml_body)
        channel = root.find("channel")
        if channel is None:
            return preferred_title, preferred_path
        item = channel.find("item")
        if item is None:
            return preferred_title, preferred_path
        link = item.findtext("link") or ""
        link = link.strip()
        if link.startswith("/content/"):
            # Try to recover a human label from the URL.
            label = link.rsplit("/", 1)[-1]
            label = urllib.parse.unquote(label)
            return label or preferred_title, link
    except Exception:
        return preferred_title, preferred_path
    return preferred_title, preferred_path


def _bench_one_zim(
    *,
    kiwix_serve: str,
    kiwix_search: str | None,
    zim_path: str,
    host: str,
    start_port: int,
    runs: int,
    warmups: int,
) -> ZimResult:
    zim_path = os.path.realpath(zim_path)
    filename_base = os.path.splitext(os.path.basename(zim_path))[0]
    zim_size_gb = os.path.getsize(zim_path) / (1024**3)

    port = _find_free_port(host, start_port=start_port)

    server_cmd = [kiwix_serve, "--address", host, "--port", str(port), zim_path]
    server_start = time.perf_counter()
    proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
    )

    try:
        ready_wall = _wait_ready(host, port, timeout_s=120.0)
        startup_s = time.perf_counter() - server_start

        # Fetch catalog info (for ZIM metadata + content root)
        _, catalog_body, _ = _http_get(host, port, "/catalog/v2/entries?count=1", headers={"Connection": "close"}, read_limit=512 * 1024)
        catalog_entry = _parse_catalog_entry(catalog_body)

        content_root = (catalog_entry.get("content_root") or "").strip() or filename_base
        language = (catalog_entry.get("language") or "").strip()
        query_pattern, suggest_term, preferred_article = _guess_queries(language, filename_base)

        article_label, article_path = _pick_article_path(
            host,
            port,
            content_root=content_root,
            preferred_title=preferred_article,
            search_pattern=query_pattern,
        )

        # HTTP benchmark cases
        q = urllib.parse.urlencode(
            {"pattern": query_pattern, "content": content_root, "format": "xml", "pageLength": "5"}
        )
        http_cases = [
            HttpCase(
                name="OPDS entries (count=1)",
                path="/catalog/v2/entries?count=1",
                headers={"Connection": "close"},
            ),
            HttpCase(
                name=f"Full-text search (XML, 5 results, pattern={query_pattern})",
                path=f"/search?{q}",
                headers={"Connection": "close"},
            ),
            HttpCase(
                name=f"Suggest (JSON, term={suggest_term}, count=3)",
                path=f"/suggest?content={urllib.parse.quote(content_root)}&term={urllib.parse.quote(suggest_term)}&count=3",
                headers={"Connection": "close"},
            ),
            HttpCase(
                name=f"Article HTML head ({article_label}, first 200KB)",
                path=article_path,
                headers={"Connection": "close", "Range": "bytes=0-200000"},
                read_limit=220_000,
            ),
        ]

        http_results = [_bench_http(host, port, case, runs=runs, warmups=warmups) for case in http_cases]

        # Server RSS (best effort)
        rss_kb = None
        try:
            code, out = _run(["ps", "-o", "rss=", "-p", str(proc.pid)])
            if code == 0:
                rss_kb = int(out.strip())
        except Exception:
            rss_kb = None

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    cli_first = None
    cli_warm = None
    if kiwix_search and os.path.exists(kiwix_search):
        cli_cmd = [kiwix_search, zim_path, query_pattern]
        cli_first = _bench_cli_once(cli_cmd)
        cli_warm = _bench_cli(cli_cmd, runs=20)

    listen_url = f"http://{host}:{port}/"
    return ZimResult(
        zim_path=zim_path,
        zim_size_gb=zim_size_gb,
        filename_base=filename_base,
        content_root=content_root,
        catalog_entry=catalog_entry,
        query_pattern=query_pattern,
        suggest_term=suggest_term,
        article_label=article_label,
        article_path=article_path,
        server_cmd=server_cmd,
        listen_url=listen_url,
        ready_wall=ready_wall,
        startup_s=startup_s,
        rss_kb=rss_kb,
        http_results=http_results,
        cli_first=cli_first,
        cli_warm=cli_warm,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark kiwix-serve / kiwix-search against a local ZIM.")
    parser.add_argument(
        "--zim",
        action="append",
        help="Path to a ZIM file (repeatable). If omitted, benchmarks the repo's English ZIM and, if present, the Chinese ZIM in ../play_zim.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Listen host for kiwix-serve (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=0, help="Port for kiwix-serve (0 = auto pick).")
    parser.add_argument("--runs", type=int, default=30, help="Runs per HTTP case (default: 30).")
    parser.add_argument("--warmups", type=int, default=3, help="Warmup runs per HTTP case (default: 3).")
    parser.add_argument("--out", default="benchmark_report.html", help="Output HTML path (default: benchmark_report.html).")
    args = parser.parse_args()

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    zim_args = list(args.zim or [])
    if not zim_args:
        zim_args.append("wikipedia_en_simple_all_maxi_2025-11.zim")
        zh_candidate = os.path.join("..", "play_zim", "wikipedia_zh_all_nopic_2025-09.zim")
        if os.path.exists(_resolve_path(repo_dir, zh_candidate)):
            zim_args.append(zh_candidate)

    zim_paths: list[str] = []
    for z in zim_args:
        resolved = _resolve_path(repo_dir, z)
        if resolved not in zim_paths:
            zim_paths.append(resolved)

    kiwix_serve = shutil.which("kiwix-serve") or os.path.join(os.path.expanduser("~"), ".local", "bin", "kiwix-serve")
    kiwix_search = shutil.which("kiwix-search") or os.path.join(os.path.expanduser("~"), ".local", "bin", "kiwix-search")

    missing = [p for p in zim_paths if not os.path.exists(p)]
    if missing:
        for p in missing:
            print(f"ZIM not found: {p}", file=sys.stderr)
        return 2
    if not os.path.exists(kiwix_serve):
        print(f"kiwix-serve not found on PATH and not at: {kiwix_serve}", file=sys.stderr)
        return 2

    host = args.host
    start_port = args.port or 8090

    # Collect environment info (best effort)
    env_info: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
        "hw_model": _best_effort_sysctl("hw.model"),
        "cpu_brand": _best_effort_sysctl("machdep.cpu.brand_string") or _best_effort_sysctl("machdep.cpu.brand"),
        "mem_bytes": _best_effort_sysctl("hw.memsize"),
    }

    # Versions
    _, kiwix_serve_ver = _run([kiwix_serve, "--version"])
    kiwix_search_ver = ""
    if os.path.exists(kiwix_search):
        _, kiwix_search_ver = _run([kiwix_search, "--version"])

    results: list[ZimResult] = []
    for p in zim_paths:
        results.append(
            _bench_one_zim(
                kiwix_serve=kiwix_serve,
                kiwix_search=kiwix_search if os.path.exists(kiwix_search) else None,
                zim_path=p,
                host=host,
                start_port=start_port,
                runs=args.runs,
                warmups=args.warmups,
            )
        )
        start_port += 1

    # Render HTML
    title = "Kiwix 本地 ZIM 性能测速报告"
    out_path = args.out
    if not os.path.isabs(out_path):
        out_path = os.path.join(repo_dir, out_path)

    def kv_row(k: str, v: Any) -> str:
        if v is None:
            v = ""
        return f"<tr><th>{_html_escape(k)}</th><td>{_html_escape(str(v))}</td></tr>"

    def times_row(label: str, stats: dict[str, float]) -> str:
        return (
            "<tr>"
            f"<th>{_html_escape(label)}</th>"
            f"<td>{int(stats['n'])}</td>"
            f"<td>{_fmt_s(stats['min'])}</td>"
            f"<td>{_fmt_s(stats['p50'])}</td>"
            f"<td>{_fmt_s(stats['mean'])}</td>"
            f"<td>{_fmt_s(stats['p95'])}</td>"
            f"<td>{_fmt_s(stats['max'])}</td>"
            f"<td>{_fmt_s(stats['stdev'])}</td>"
            "</tr>"
        )

    # ZIM summary rows
    zims_rows = []
    for r in results:
        ce = r.catalog_entry
        zims_rows.append(
            "<tr>"
            f"<td>{_html_escape(ce.get('title') or r.filename_base)}</td>"
            f"<td><code>{_html_escape(r.content_root)}</code></td>"
            f"<td>{_html_escape(ce.get('language', ''))}</td>"
            f"<td>{_html_escape(ce.get('flavour', ''))}</td>"
            f"<td>{_html_escape(ce.get('category', ''))}</td>"
            f"<td>{_html_escape(f'{r.zim_size_gb:.2f}')} GB</td>"
            f"<td class='small'><code>{_html_escape(r.zim_path)}</code></td>"
            "</tr>"
        )

    # Startup rows
    startup_rows = []
    for r in results:
        rss_pretty = f"{r.rss_kb/1024:.1f} MB" if r.rss_kb is not None else ""
        startup_rows.append(
            "<tr>"
            f"<td>{_html_escape(r.catalog_entry.get('title') or r.filename_base)}</td>"
            f"<td><code>{_html_escape(r.content_root)}</code></td>"
            f"<td>{_html_escape(r.listen_url)}</td>"
            f"<td>{_fmt_s(r.startup_s)}</td>"
            f"<td>{_html_escape(rss_pretty)}</td>"
            f"<td class='small'><code>{_html_escape(' '.join(r.server_cmd))}</code></td>"
            "</tr>"
        )

    # HTTP combined rows
    http_table_rows = []
    for zim in results:
        zim_label = zim.catalog_entry.get("title") or zim.filename_base
        for h in zim.http_results:
            t = h["times"]
            status_counts = ", ".join(f"{k}:{v}" for k, v in sorted(h["status_counts"].items()))
            http_table_rows.append(
                "<tr>"
                f"<td>{_html_escape(zim_label)}<br/><span class='small'><code>{_html_escape(zim.content_root)}</code></span></td>"
                f"<td>{_html_escape(h['name'])}</td>"
                f"<td><code>{_html_escape(h['path'])}</code></td>"
                f"<td>{int(h['runs'])}</td>"
                f"<td>{_fmt_s(t['p50'])}</td>"
                f"<td>{_fmt_s(t['mean'])}</td>"
                f"<td>{_fmt_s(t['p95'])}</td>"
                f"<td>{_fmt_s(t['max'])}</td>"
                f"<td>{int(h['bytes_mean'])}</td>"
                f"<td><code>{_html_escape(status_counts)}</code></td>"
                "</tr>"
            )

    # CLI combined block
    cli_rows = []
    have_cli = any(r.cli_warm is not None for r in results)
    if have_cli:
        for r in results:
            if r.cli_warm is None:
                continue
            zim_label = r.catalog_entry.get("title") or r.filename_base
            warm = r.cli_warm
            t = warm["times"]
            first_s = ""
            if r.cli_first is not None:
                first_s = _fmt_s(float(r.cli_first.get("time_s", 0.0)))
            rc_counts = ", ".join(f"{k}:{v}" for k, v in sorted(warm["return_codes"].items()))
            cli_rows.append(
                "<tr>"
                f"<td>{_html_escape(zim_label)}<br/><span class='small'><code>{_html_escape(r.content_root)}</code></span></td>"
                f"<td><code>{_html_escape(r.query_pattern)}</code></td>"
                f"<td>{_html_escape(first_s)}</td>"
                f"<td>{int(t['n'])}</td>"
                f"<td>{_fmt_s(t['p50'])}</td>"
                f"<td>{_fmt_s(t['mean'])}</td>"
                f"<td>{_fmt_s(t['p95'])}</td>"
                f"<td>{_fmt_s(t['max'])}</td>"
                f"<td><code>{_html_escape(rc_counts)}</code></td>"
                "</tr>"
            )

    # Summary (top-of-report)
    summary_rows = []
    for r in results:
        title_label = r.catalog_entry.get("title") or r.filename_base
        search_p50 = _first_case_p50(r, "Full-text search")
        suggest_p50 = _first_case_p50(r, "Suggest")
        article_p50 = _first_case_p50(r, "Article HTML head")
        cli_p50 = None
        if r.cli_warm is not None:
            try:
                cli_p50 = float(r.cli_warm["times"]["p50"])
            except Exception:
                cli_p50 = None
        summary_rows.append(
            "<tr>"
            f"<td>{_html_escape(title_label)}<br/><span class='small'><code>{_html_escape(r.content_root)}</code></span></td>"
            f"<td>{_html_escape(r.catalog_entry.get('language',''))}</td>"
            f"<td>{_html_escape(r.catalog_entry.get('flavour',''))}</td>"
            f"<td>{_html_escape(f'{r.zim_size_gb:.2f}')} GB</td>"
            f"<td><code>{_html_escape(r.query_pattern)}</code></td>"
            f"<td>{_fmt_s(r.startup_s)}</td>"
            f"<td>{_fmt_s(search_p50) if search_p50 is not None else ''}</td>"
            f"<td>{_fmt_s(suggest_p50) if suggest_p50 is not None else ''}</td>"
            f"<td>{_fmt_s(article_p50) if article_p50 is not None else ''}</td>"
            f"<td>{_fmt_s(cli_p50) if cli_p50 is not None else ''}</td>"
            "</tr>"
        )

    summary_bullets = []
    if len(results) >= 2:
        # Compare p50 for key metrics
        def _metric_bullet(prefix: str, label: str) -> None:
            vals = []
            for r in results:
                v = _first_case_p50(r, prefix)
                if v is not None:
                    vals.append((v, r.catalog_entry.get("title") or r.filename_base))
            if len(vals) < 2:
                return
            fast_v, fast_name = min(vals, key=lambda x: x[0])
            slow_v, slow_name = max(vals, key=lambda x: x[0])
            ratio = _ratio_slow_text(fast_v, slow_v)
            summary_bullets.append(
                f"<li>{_html_escape(label)}：最快 {_html_escape(fast_name)}（{_fmt_s(fast_v)}），最慢 {_html_escape(slow_name)}（{_fmt_s(slow_v)}），约 {ratio} 差异（p50）。</li>"
            )

        _metric_bullet("Full-text search", "全文搜索")
        _metric_bullet("Suggest", "Suggest")

        # CLI p50 compare
        cli_vals = []
        for r in results:
            if r.cli_warm is None:
                continue
            try:
                v = float(r.cli_warm["times"]["p50"])
            except Exception:
                continue
            cli_vals.append((v, r.catalog_entry.get("title") or r.filename_base))
        if len(cli_vals) >= 2:
            fast_v, fast_name = min(cli_vals, key=lambda x: x[0])
            slow_v, slow_name = max(cli_vals, key=lambda x: x[0])
            ratio = _ratio_slow_text(fast_v, slow_v)
            summary_bullets.append(
                f"<li>CLI（kiwix-search）：最快 {_html_escape(fast_name)}（{_fmt_s(fast_v)}），最慢 {_html_escape(slow_name)}（{_fmt_s(slow_v)}），约 {ratio} 差异（p50）。</li>"
            )

    mem_pretty = ""
    if env_info.get("mem_bytes"):
        try:
            mem_pretty = f"{int(env_info['mem_bytes'])/ (1024**3):.1f} GB"
        except Exception:
            mem_pretty = str(env_info["mem_bytes"])

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{_html_escape(title)}</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --panel: #111827;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --border: #1f2937;
      --accent: #60a5fa;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .wrap {{
      max-width: 980px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    h1 {{
      font-size: 22px;
      margin: 0 0 8px;
    }}
    h2 {{
      font-size: 16px;
      margin: 22px 0 10px;
    }}
    p, li {{
      color: var(--muted);
      margin: 8px 0;
    }}
    code {{
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 6px;
      color: var(--text);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
    }}
    th, td {{
      padding: 10px 10px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{
      color: var(--text);
      width: 32%;
      font-weight: 600;
    }}
    tr:last-child td, tr:last-child th {{
      border-bottom: none;
    }}
    .small {{
      font-size: 12px;
      color: var(--muted);
    }}
    .pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(96,165,250,0.12);
      border: 1px solid rgba(96,165,250,0.25);
      color: var(--text);
      font-size: 12px;
      margin-left: 8px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{_html_escape(title)} <span class="pill">本机测得</span></h1>
    <p class="small">生成时间：{_html_escape(env_info.get("generated_at",""))}</p>

    <h2>总结</h2>
    <p class="small">表格里的延迟默认看 p50（中位数），HTTP 为 <code>Connection: close</code>；全文搜索 / suggest 的关键词会按语言自动选（英文用 <code>earth</code>，中文用 <code>地球</code>）。</p>
    <table>
      <thead>
        <tr>
          <th>ZIM</th>
          <th>Lang</th>
          <th>Flavour</th>
          <th>Size</th>
          <th>Pattern</th>
          <th>Startup</th>
          <th>Search p50</th>
          <th>Suggest p50</th>
          <th>Article p50</th>
          <th>kiwix-search p50</th>
        </tr>
      </thead>
      <tbody>
        {"".join(summary_rows)}
      </tbody>
    </table>
    {"<ul>" + "".join(summary_bullets) + "</ul>" if summary_bullets else ""}

    <div class="grid">
      <div class="card">
        <h2>环境</h2>
        <table>
          {kv_row("OS", env_info.get("platform"))}
          {kv_row("CPU", env_info.get("cpu_brand") or env_info.get("machine"))}
          {kv_row("CPU cores", env_info.get("cpu_count"))}
          {kv_row("Memory", mem_pretty)}
          {kv_row("Python", env_info.get("python"))}
        </table>
      </div>
      <div class="card">
        <h2>运行参数</h2>
        <table>
          {kv_row("Host", host)}
          {kv_row("Runs / warmups", f"{int(args.runs)} / {int(args.warmups)}")}
        </table>
      </div>
    </div>

    <h2>ZIM 列表</h2>
    <table>
      <thead>
        <tr>
          <th>Title</th>
          <th>ZIMNAME</th>
          <th>Lang</th>
          <th>Flavour</th>
          <th>Category</th>
          <th>Size</th>
          <th>Path</th>
        </tr>
      </thead>
      <tbody>
        {"".join(zims_rows)}
      </tbody>
    </table>

    <h2>版本</h2>
    <table>
      {kv_row("kiwix-serve", kiwix_serve_ver)}
      {kv_row("kiwix-search", kiwix_search_ver)}
    </table>

    <h2>Server 启动</h2>
    <table>
      <thead>
        <tr>
          <th>Title</th>
          <th>ZIMNAME</th>
          <th>Listen URL</th>
          <th>Startup</th>
          <th>RSS</th>
          <th>Command</th>
        </tr>
      </thead>
      <tbody>
        {"".join(startup_rows)}
      </tbody>
    </table>

    <h2>HTTP 延迟（{int(args.runs)} runs + {int(args.warmups)} warmups）</h2>
    <table>
      <thead>
        <tr>
          <th>ZIM</th>
          <th>Case</th>
          <th>Path</th>
          <th>Runs</th>
          <th>p50</th>
          <th>mean</th>
          <th>p95</th>
          <th>max</th>
          <th>Bytes(mean)</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {"".join(http_table_rows)}
      </tbody>
    </table>

    {"<h2>CLI（kiwix-search）</h2><table><thead><tr><th>ZIM</th><th>Pattern</th><th>First</th><th>Runs</th><th>p50</th><th>mean</th><th>p95</th><th>max</th><th>Return codes</th></tr></thead><tbody>"
      + "".join(cli_rows) + "</tbody></table>" if have_cli else ""}

    <h2>说明</h2>
    <ul>
      <li>数值是本机实测，受磁盘（SSD/HDD）、系统缓存、CPU、电源模式影响很大。</li>
      <li>HTTP 请求都带了 <code>Connection: close</code> 以减少 keep-alive 对测量的干扰；词条页只拉取前 200KB。</li>
      <li>全文搜索依赖 ZIM 是否包含全文索引（如果 ZIM 没有全文索引，<code>/search</code> 可能返回错误或空结果）。</li>
    </ul>

    <p class="small">生成脚本：<code>bench.py</code></p>
  </div>
</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)

    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
