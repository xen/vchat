#!/usr/bin/env python3
import argparse
import asyncio
import os
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import advertools as adv
import aiohttp
import matplotlib.pyplot as plt
import pandas as pd

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:15.0) "
    "Gecko/20100101 Firefox/15.0.1 vChat Crawler"
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit sitemap URLs and build a visual report")
    parser.add_argument("sitemap_url", help="Root sitemap URL (sitemap.xml or sitemap index)")
    parser.add_argument("--out-dir", default="/Users/xen/Dev/sber/vchat/data", help="Base output directory")
    parser.add_argument("--concurrency", type=int, default=12, help="HTTP concurrency for URL checks")
    parser.add_argument("--timeout", type=int, default=12, help="Request timeout seconds")
    parser.add_argument("--user-agent", default=DEFAULT_UA, help="User-Agent")
    parser.add_argument("--sitemap-retries", type=int, default=4, help="Retries per sitemap download")
    parser.add_argument("--url-retries", type=int, default=2, help="Retries per URL check on failures")
    parser.add_argument("--tor", action="store_true", help="Use Tor SOCKS proxy for all HTTP requests")
    parser.add_argument("--tor-proxy-url", default="socks5://127.0.0.1:9050", help="Tor SOCKS proxy URL")
    parser.add_argument("--tor-control-host", default="127.0.0.1", help="Tor control host")
    parser.add_argument("--tor-control-port", type=int, default=9051, help="Tor control port")
    parser.add_argument("--tor-control-password", default="", help="Tor control password")
    parser.add_argument("--tor-newnym-wait", type=float, default=5.0, help="Seconds to wait after NEWNYM")
    return parser.parse_args()


def is_timeout_error(msg: str | None) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return "timeout" in low or "timed out" in low


def path_prefix(url: str) -> str:
    try:
        p = urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        if not parts:
            return "/"
        return "/" + "/".join(parts[:2])
    except Exception:
        return "(invalid)"


async def download_text(session: aiohttp.ClientSession, url: str, timeout_s: int, retries: int) -> tuple[str | None, str | None]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
                if resp.status >= 400:
                    err = f"HTTP {resp.status}"
                    print(f"[SITEMAP_FAIL] {url} ({err})", flush=True)
                    return None, err
                text = await resp.text(errors="ignore")
                return text, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[SITEMAP_RETRY] {url} attempt {attempt}/{retries} failed: {last_err}", flush=True)
            await asyncio.sleep(min(20, attempt * 2))
    print(f"[SITEMAP_FAIL] {url} ({last_err})", flush=True)
    return None, last_err


def parse_sitemap_children(xml_text: str) -> tuple[str, list[str]]:
    root = ET.fromstring(xml_text)
    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        locs = [el.text.strip() for el in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS) if el.text]
        return "index", locs
    if tag.endswith("urlset"):
        return "urlset", []
    return "unknown", []


def local_file_url(path: Path) -> str:
    return f"file://{path}"


class TorController:
    def __init__(self, host: str, port: int, password: str, wait_after_newnym: float):
        self.host = host
        self.port = port
        self.password = password
        self.wait_after_newnym = wait_after_newnym
        self._lock = asyncio.Lock()

    async def rotate(self, reason: str, url: str) -> bool:
        async with self._lock:
            print(f"[TOR] rotate requested ({reason}) for {url}", flush=True)
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                writer.write(b"PROTOCOLINFO 1\r\n")
                await writer.drain()
                protocol_lines: list[str] = []
                while True:
                    line = (await reader.readline()).decode("utf-8", errors="ignore").strip()
                    if not line:
                        break
                    protocol_lines.append(line)
                    if line.startswith("250 OK") or line.startswith("5"):
                        break

                methods = ""
                cookie_file = ""
                for line in protocol_lines:
                    if line.startswith("250-AUTH "):
                        methods = line
                        marker = 'COOKIEFILE="'
                        if marker in line:
                            start = line.index(marker) + len(marker)
                            end = line.find('"', start)
                            if end > start:
                                cookie_file = line[start:end]
                        break

                auth_cmd = None
                if self.password:
                    auth_cmd = f'AUTHENTICATE "{self.password}"\r\n'
                elif "METHODS=NULL" in methods or ",NULL" in methods:
                    auth_cmd = "AUTHENTICATE\r\n"
                elif "COOKIE" in methods and cookie_file:
                    cookie_path = cookie_file.replace("\\\\", "\\")
                    if not os.path.exists(cookie_path):
                        raise RuntimeError(f"cookie file not found: {cookie_path}")
                    cookie_hex = Path(cookie_path).read_bytes().hex()
                    auth_cmd = f"AUTHENTICATE {cookie_hex}\r\n"
                else:
                    raise RuntimeError(f"unsupported auth methods from Tor: {methods or 'unknown'}")

                writer.write(auth_cmd.encode("utf-8"))
                await writer.drain()
                auth_resp = (await reader.readline()).decode("utf-8", errors="ignore").strip()
                if not auth_resp.startswith("250"):
                    raise RuntimeError(f"AUTH failed: {auth_resp}")

                writer.write(b"SIGNAL NEWNYM\r\n")
                await writer.drain()
                nym_resp = (await reader.readline()).decode("utf-8", errors="ignore").strip()
                if not nym_resp.startswith("250"):
                    raise RuntimeError(f"NEWNYM failed: {nym_resp}")

                writer.write(b"QUIT\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(self.wait_after_newnym)
                print(f"[TOR] new circuit applied for {url}", flush=True)
                return True
            except Exception as e:
                print(f"[TOR_FAIL] cannot rotate circuit for {url}: {type(e).__name__}: {e}", flush=True)
                return False


def build_connector(limit: int, use_tor: bool, tor_proxy_url: str):
    if use_tor:
        try:
            from aiohttp_socks import ProxyConnector
        except Exception as e:
            raise RuntimeError(
                "Tor mode requires aiohttp_socks. Install: pip install aiohttp-socks"
            ) from e
        return ProxyConnector.from_url(tor_proxy_url, limit=limit, ssl=False)
    return aiohttp.TCPConnector(ssl=False, limit=limit)


async def download_text(
    session: aiohttp.ClientSession,
    url: str,
    timeout_s: int,
    retries: int,
    tor_controller: TorController | None,
) -> tuple[str | None, str | None]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    last_err = None
    total_attempts = max(1, retries)
    for attempt in range(1, total_attempts + 1):
        try:
            async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
                if resp.status >= 400:
                    err = f"HTTP {resp.status}"
                    if attempt < total_attempts:
                        print(f"[SITEMAP_RETRY] {url} attempt {attempt}/{total_attempts} failed: {err}", flush=True)
                        if tor_controller:
                            await tor_controller.rotate("sitemap_http_error", url)
                        await asyncio.sleep(min(20, attempt * 2))
                        continue
                    print(f"[SITEMAP_FAIL] {url} ({err})", flush=True)
                    return None, err
                text = await resp.text(errors="ignore")
                return text, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[SITEMAP_RETRY] {url} attempt {attempt}/{total_attempts} failed: {last_err}", flush=True)
            if attempt < total_attempts:
                if tor_controller:
                    await tor_controller.rotate("sitemap_exception", url)
                await asyncio.sleep(min(20, attempt * 2))
    print(f"[SITEMAP_FAIL] {url} ({last_err})", flush=True)
    return None, last_err


async def collect_sitemaps(
    root_url: str,
    out_dir: Path,
    ua: str,
    timeout_s: int,
    retries: int,
    use_tor: bool,
    tor_proxy_url: str,
    tor_controller: TorController | None,
):
    sitemaps_dir = out_dir / "downloaded_sitemaps"
    sitemaps_dir.mkdir(parents=True, exist_ok=True)

    queue = deque([root_url])
    seen = set()
    downloaded = []
    failed = []

    headers = {"User-Agent": ua}
    connector = build_connector(limit=8, use_tor=use_tor, tor_proxy_url=tor_proxy_url)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        processed = 0
        while queue:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            processed += 1
            total_known = processed + len(queue)
            print(f"[SITEMAP {processed}/{total_known}] downloading: {url}", flush=True)

            text, err = await download_text(
                session,
                url,
                timeout_s=timeout_s,
                retries=retries,
                tor_controller=tor_controller,
            )
            if err or text is None:
                failed.append({"url": url, "stage": "download", "error": err or "unknown"})
                continue

            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            file_path = sitemaps_dir / f"{digest}.xml"
            file_path.write_text(text, encoding="utf-8")

            try:
                stype, child_sitemaps = parse_sitemap_children(text)
            except Exception as e:
                failed.append({"url": url, "stage": "xml_parse", "error": f"{type(e).__name__}: {e}"})
                print(f"[SITEMAP_FAIL] {url} (xml parse: {e})", flush=True)
                continue

            downloaded.append({"url": url, "type": stype, "file_path": str(file_path)})
            if stype == "index":
                for child in child_sitemaps:
                    if child not in seen:
                        queue.append(child)

    return downloaded, failed


def build_sitemap_df(downloaded: list[dict], failed: list[dict]) -> pd.DataFrame:
    rows = []
    for item in downloaded:
        if item["type"] != "urlset":
            continue
        file_url = local_file_url(Path(item["file_path"]))
        try:
            df = adv.sitemap_to_df(file_url, recursive=False)
            if "loc" not in df.columns:
                failed.append({"url": item["url"], "stage": "advertools_parse", "error": "missing loc"})
                print(f"[SITEMAP_FAIL] {item['url']} (advertools missing loc)", flush=True)
                continue
            df["source_sitemap_url"] = item["url"]
            rows.append(df)
        except Exception as e:
            failed.append({"url": item["url"], "stage": "advertools_parse", "error": f"{type(e).__name__}: {e}"})
            print(f"[SITEMAP_FAIL] {item['url']} (advertools parse: {e})", flush=True)

    if not rows:
        return pd.DataFrame(columns=["loc", "source_sitemap_url"])

    merged = pd.concat(rows, ignore_index=True)
    merged["loc"] = merged["loc"].astype(str).str.strip()
    merged = merged[merged["loc"].ne("")]
    return merged


async def run_checks(
    urls: list[str],
    user_agent: str,
    concurrency: int,
    timeout_s: int,
    retries: int,
    use_tor: bool,
    tor_proxy_url: str,
    tor_controller: TorController | None,
) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": user_agent}

    async def fetch_status(session: aiohttp.ClientSession, url: str) -> dict:
        row = {
            "url": url,
            "http_status": None,
            "final_url": None,
            "content_type": None,
            "error": None,
            "method": None,
            "attempts": 0,
        }
        async with sem:
            total_attempts = max(1, retries + 1)
            for attempt in range(1, total_attempts + 1):
                row["attempts"] = attempt
                row["error"] = None
                try:
                    print(f"[URL_CHECK_START {attempt}/{total_attempts}] {url}", flush=True)
                    async with session.head(url, allow_redirects=True, timeout=timeout) as resp:
                        row.update(
                            {
                                "http_status": resp.status,
                                "final_url": str(resp.url),
                                "content_type": resp.headers.get("Content-Type"),
                                "method": "HEAD",
                            }
                        )
                        await resp.release()

                    if row["http_status"] in {400, 403, 405, 429, 500, 501, 502, 503, 504}:
                        async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
                            row.update(
                                {
                                    "http_status": resp.status,
                                    "final_url": str(resp.url),
                                    "content_type": resp.headers.get("Content-Type"),
                                    "method": "GET",
                                }
                            )
                            await resp.release()
                except Exception as e:
                    row["error"] = f"{type(e).__name__}: {e}"

                should_retry = row["error"] is not None or row["http_status"] in {403, 429, 503}
                if not should_retry or attempt >= total_attempts:
                    break

                print(
                    f"[URL_RETRY] {url} attempt {attempt}/{total_attempts} failed "
                    f"(status={row['http_status']} error={row['error']})",
                    flush=True,
                )
                if tor_controller:
                    await tor_controller.rotate("url_retry", url)
                await asyncio.sleep(min(20, attempt * 2))

            if row["error"]:
                if is_timeout_error(row["error"]):
                    print(f"[URL_TIMEOUT] {url}", flush=True)
                else:
                    print(f"[URL_FAIL] {url} ({row['error']})", flush=True)
        return row

    connector = build_connector(limit=concurrency, use_tor=use_tor, tor_proxy_url=tor_proxy_url)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        tasks = [asyncio.create_task(fetch_status(session, u)) for u in urls]
        total = len(tasks)
        done = 0
        results = []
        for fut in asyncio.as_completed(tasks):
            row = await fut
            results.append(row)
            done += 1
            print(f"\rChecked {done}/{total} | last: {row['url']}", end="", flush=True)
        print("", flush=True)
        return results


def main() -> None:
    args = parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) / f"sitemap_audit_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    tor_controller = None
    if args.tor:
        tor_controller = TorController(
            host=args.tor_control_host,
            port=args.tor_control_port,
            password=args.tor_control_password,
            wait_after_newnym=args.tor_newnym_wait,
        )
        print(f"[TOR] enabled via {args.tor_proxy_url}", flush=True)

    print(f"OUT_DIR={out_dir}", flush=True)
    print("Downloading sitemap files...", flush=True)

    downloaded, failed_sitemaps = asyncio.run(
        collect_sitemaps(
            root_url=args.sitemap_url,
            out_dir=out_dir,
            ua=args.user_agent,
            timeout_s=args.timeout,
            retries=args.sitemap_retries,
            use_tor=args.tor,
            tor_proxy_url=args.tor_proxy_url,
            tor_controller=tor_controller,
        )
    )

    pd.DataFrame(downloaded).to_csv(out_dir / "downloaded_sitemaps.csv", index=False)
    failed_sitemaps_df = pd.DataFrame(failed_sitemaps)
    failed_sitemaps_df.to_csv(out_dir / "failed_sitemaps.csv", index=False)
    failed_sitemaps_timeout_path = out_dir / "failed_sitemaps_timeouts.csv"
    if not failed_sitemaps_df.empty:
        failed_sitemaps_df[failed_sitemaps_df["error"].apply(is_timeout_error)].to_csv(
            failed_sitemaps_timeout_path, index=False
        )
    else:
        pd.DataFrame(columns=["url", "stage", "error"]).to_csv(
            failed_sitemaps_timeout_path, index=False
        )

    print(f"Downloaded sitemaps: {len(downloaded)}; failed: {len(failed_sitemaps)}", flush=True)
    print("Parsing downloaded sitemaps with advertools...", flush=True)
    sitemap_df = build_sitemap_df(downloaded, failed_sitemaps)
    sitemap_df.to_csv(out_dir / "sitemap_urls_raw.csv", index=False)

    if sitemap_df.empty:
        raise RuntimeError("No URLs parsed from downloaded sitemaps; check failed_sitemaps.csv")

    urls = pd.Series(sitemap_df["loc"].dropna().unique()).tolist()
    print(f"UNIQUE_URLS={len(urls)}", flush=True)

    results = asyncio.run(
        run_checks(
            urls,
            args.user_agent,
            args.concurrency,
            args.timeout,
            retries=args.url_retries,
            use_tor=args.tor,
            tor_proxy_url=args.tor_proxy_url,
            tor_controller=tor_controller,
        )
    )
    status_df = pd.DataFrame(results)
    status_df.to_csv(out_dir / "url_status_checks.csv", index=False)

    timeout_urls = status_df[status_df["error"].apply(is_timeout_error)]
    url_timeouts_path = out_dir / "url_timeouts.csv"
    if timeout_urls.empty:
        pd.DataFrame(columns=status_df.columns).to_csv(url_timeouts_path, index=False)
    else:
        timeout_urls.to_csv(url_timeouts_path, index=False)

    merged = status_df.merge(
        sitemap_df.drop_duplicates(subset=["loc"]),
        left_on="url",
        right_on="loc",
        how="left",
    )
    merged.to_csv(out_dir / "sitemap_urls_with_status.csv", index=False)

    status_series = merged["http_status"].fillna(0).astype(int)
    status_group = status_series.apply(lambda s: f"{s//100}xx" if s else "error")
    status_counts = status_group.value_counts().sort_index()

    broken_mask = (
        merged["error"].notna()
        | merged["http_status"].isna()
        | (merged["http_status"] >= 400)
    )
    broken_df = merged[broken_mask].copy()
    broken_df["path_prefix"] = broken_df["url"].map(path_prefix)

    prefix_counts = broken_df["path_prefix"].value_counts().head(30)
    prefix_counts.to_csv(out_dir / "broken_prefix_counts.csv", header=["count"])

    summary = {
        "root_sitemap": args.sitemap_url,
        "generated_at": datetime.now().isoformat(),
        "downloaded_sitemaps": int(len(downloaded)),
        "failed_sitemaps": int(len(failed_sitemaps_df)),
        "failed_sitemaps_timeouts": int(len(failed_sitemaps_df[failed_sitemaps_df["error"].apply(is_timeout_error)])) if not failed_sitemaps_df.empty else 0,
        "total_raw_rows": int(len(sitemap_df)),
        "total_unique_urls": int(len(urls)),
        "ok_2xx": int(((merged["http_status"] >= 200) & (merged["http_status"] < 300)).sum()),
        "redirect_3xx": int(((merged["http_status"] >= 300) & (merged["http_status"] < 400)).sum()),
        "client_error_4xx": int(((merged["http_status"] >= 400) & (merged["http_status"] < 500)).sum()),
        "server_error_5xx": int(((merged["http_status"] >= 500) & (merged["http_status"] < 600)).sum()),
        "network_or_other_errors": int(merged["error"].notna().sum()),
        "url_timeouts": int(len(timeout_urls)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    plt.figure(figsize=(9, 5))
    status_counts.plot(kind="bar", color=["#2a9d8f", "#e9c46a", "#f4a261", "#e76f51", "#7a7a7a"][: len(status_counts)])
    plt.title("Распределение URL по классам HTTP-статусов")
    plt.xlabel("Класс статуса")
    plt.ylabel("Количество URL")
    plt.tight_layout()
    plt.savefig(out_dir / "status_class_distribution.png", dpi=150)
    plt.close()

    if not prefix_counts.empty:
        plt.figure(figsize=(11, 7))
        prefix_counts.sort_values().plot(kind="barh", color="#e76f51")
        plt.title("Топ префиксов URL с ошибками (4xx/5xx/исключения)")
        plt.xlabel("Количество URL")
        plt.ylabel("Префикс пути")
        plt.tight_layout()
        plt.savefig(out_dir / "broken_prefixes_top30.png", dpi=150)
        plt.close()

    broken_df[["url", "http_status", "final_url", "error", "method"]].head(300).to_csv(
        out_dir / "broken_urls_sample_top300.csv", index=False
    )

    report_md = f"""# Sitemap Audit Report\n\n- Root sitemap: `{args.sitemap_url}`\n- Generated: `{summary['generated_at']}`\n- Downloaded sitemaps: **{summary['downloaded_sitemaps']}**\n- Failed sitemap downloads: **{summary['failed_sitemaps']}**\n- Failed sitemap timeouts: **{summary['failed_sitemaps_timeouts']}**\n- Unique URLs discovered: **{summary['total_unique_urls']}**\n\n## Status Summary\n\n- 2xx: **{summary['ok_2xx']}**\n- 3xx: **{summary['redirect_3xx']}**\n- 4xx: **{summary['client_error_4xx']}**\n- 5xx: **{summary['server_error_5xx']}**\n- URL timeout errors: **{summary['url_timeouts']}**\n- Other errors: **{summary['network_or_other_errors']}**\n"""
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")

    html = f"""<!doctype html>
<html lang=\"ru\"><head><meta charset=\"utf-8\"><title>Sitemap Audit</title>
<style>body{{font-family:Arial,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}}img{{max-width:100%;height:auto;border:1px solid #ddd}}code{{background:#f3f3f3;padding:2px 4px}}</style>
</head><body>
<h1>Sitemap Audit</h1>
<p><b>Root sitemap:</b> <code>{args.sitemap_url}</code><br>
<b>Generated:</b> {summary['generated_at']}<br>
<b>Downloaded sitemaps:</b> {summary['downloaded_sitemaps']}<br>
<b>Failed sitemap downloads:</b> {summary['failed_sitemaps']}<br>
<b>Unique URLs discovered:</b> {summary['total_unique_urls']}</p>
<ul>
<li>2xx: <b>{summary['ok_2xx']}</b></li>
<li>3xx: <b>{summary['redirect_3xx']}</b></li>
<li>4xx: <b>{summary['client_error_4xx']}</b></li>
<li>5xx: <b>{summary['server_error_5xx']}</b></li>
<li>URL timeouts: <b>{summary['url_timeouts']}</b></li>
<li>Other errors: <b>{summary['network_or_other_errors']}</b></li>
</ul>
<h2>Распределение статус-кодов</h2>
<img src=\"status_class_distribution.png\" alt=\"status classes\">
<h2>Топ проблемных префиксов</h2>
<img src=\"broken_prefixes_top30.png\" alt=\"broken prefixes\">
<p>Файлы с проблемами: <code>failed_sitemaps.csv</code>, <code>failed_sitemaps_timeouts.csv</code>, <code>url_timeouts.csv</code>.</p>
</body></html>"""
    (out_dir / "report.html").write_text(html, encoding="utf-8")

    print("DONE", flush=True)
    print(f"REPORT_HTML={out_dir / 'report.html'}", flush=True)
    print(f"FAILED_SITEMAPS={out_dir / 'failed_sitemaps.csv'}", flush=True)
    print(f"FAILED_SITEMAPS_TIMEOUTS={failed_sitemaps_timeout_path}", flush=True)
    print(f"URL_TIMEOUTS={url_timeouts_path}", flush=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
