#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 akshare / aktools 的 pip 包最新版本，并维护本地版本记录文件。

行为：
  * 从包索引（默认 PyPI JSON API）查询指定包的最新版本；
  * 与本地版本记录文件（默认仓库根目录的 versions.json）比对；
  * 发现新版本时**自动更新**版本记录文件（原子写入，内容无变化则不落盘，避免无谓的 git diff）。

版本记录文件同时被 CI 用作「是否需要构建 Docker 镜像」的判定依据：
记录文件发生变化 => 触发镜像构建与推送（见 .github/workflows/docker-publish.yml）。

用法：
  python scripts/check_versions.py                     # 检查并更新版本记录
  python scripts/check_versions.py --dry-run           # 只检查、只打印，不写文件
  python scripts/check_versions.py --check             # 只检查；有更新时退出码为 1
  python scripts/check_versions.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple
  python scripts/check_versions.py --packages akshare,aktools,pandas --verbose

退出码：
  0  正常（--check 模式下表示已是最新）
  1  运行异常（网络失败等）
  2  --check 模式下发现可更新的版本
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_PACKAGES = ("akshare", "aktools")
DEFAULT_JSON_BASE = "https://pypi.org/pypi"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "versions.json"
USER_AGENT = "aktools-version-check/1.0 (+https://github.com/akiakise/aktools)"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    """当前 UTC 时间，ISO 8601 格式（秒级，Z 结尾）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str, *, verbose_only: bool = False, verbose: bool = False) -> None:
    if verbose_only and not verbose:
        return
    print(message, flush=True)


def normalize_version(version: str) -> str:
    """把 1.2.0 -> 1.2.0、v1.2 -> 1.2 之类的形式统一，便于比较。"""
    return version.strip().lstrip("vV")


def version_key(version: str) -> tuple:
    """轻量版本号比较键：数字段按数值比较，非数字段按字符串比较。

    仅用于排序/比较「新版本是否比旧版本更高」，不依赖 packaging 包。
    """
    parts = re.split(r"[._\-+]", normalize_version(version).lower())
    key: list[tuple[int, Any]] = []
    for part in parts:
        if not part:
            continue
        for chunk in re.findall(r"\d+|[a-z]+", part):
            if chunk.isdigit():
                key.append((1, int(chunk), ""))
            else:
                key.append((0, 0, chunk))
    return tuple(key)


def is_newer(candidate: str, current: str | None) -> bool:
    if not current:
        return True
    return version_key(candidate) > version_key(current)


# --------------------------------------------------------------------------- #
# 版本查询
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 源
        return response.read()


def fetch_from_json_api(package: str, base: str, timeout: float) -> dict[str, Any]:
    """通过 PEP 691 之外的 PyPI JSON API（/pypi/<pkg>/json）查询。"""
    url = f"{base.rstrip('/')}/{package}/json"
    payload = json.loads(http_get(url, timeout).decode("utf-8"))
    info = payload.get("info") or {}
    latest = normalize_version(str(info.get("version") or ""))
    if not latest:
        raise RuntimeError(f"{package}: PyPI JSON 响应中缺少 info.version")

    release_time: str | None = None
    files = payload.get("releases", {}).get(latest) or []
    stamps = [
        f.get("upload_time_iso_8601") or f.get("upload_time")
        for f in files
        if isinstance(f, dict)
    ]
    stamps = [s for s in stamps if s]
    if stamps:
        release_time = min(stamps)
    return {"latest": latest, "release_time": release_time, "source": url}


def parse_version_from_filename(filename: str) -> str | None:
    """从 PyPI 分发文件名中提取版本号。

    *.whl  : {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl -> 第 2 段
    *.tar.gz / *.zip : {name}-{version}.tar.gz -> 去掉扩展名后最后一个 '-' 之后的内容
    包名本身可能含 '-'，因此 sdist 从右侧切分。
    """
    name = filename.strip()
    is_wheel = name.endswith(".whl")
    for suffix in (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break

    if is_wheel:
        parts = name.split("-")
        candidate = parts[1] if len(parts) >= 5 else None
    else:
        candidate = name.rsplit("-", 1)[-1] if "-" in name else None

    if not candidate:
        return None
    candidate = candidate.strip()
    # PEP 440 风格校验：必须数字开头，允许字母/点/下划线/加号/感叹号/连字符
    if not re.fullmatch(r"[0-9][0-9A-Za-z._+!\-]*", candidate):
        return None
    if not re.search(r"\d", candidate):
        return None
    return normalize_version(candidate)


def fetch_from_simple_index(package: str, index_url: str, timeout: float) -> dict[str, Any]:
    """通过 PEP 503 简单索引（国内镜像常用）解析可用版本。"""
    url = f"{index_url.rstrip('/')}/{package}/"
    html = http_get(url, timeout).decode("utf-8", errors="replace")
    # 兼容 <a href="...">、data-requires-python 等属性，只取标签之间的文件名
    filenames = re.findall(r">([^<>]+?\.(?:whl|tar\.gz|zip|tar\.bz2|tgz))<", html)
    candidates = {v for v in (parse_version_from_filename(f) for f in filenames) if v}
    if not candidates:
        raise RuntimeError(f"{package}: 无法从简单索引 {url} 解析出版本号")
    latest = max(candidates, key=version_key)
    return {"latest": latest, "release_time": None, "source": url}


def fetch_latest(package: str, args: argparse.Namespace) -> dict[str, Any]:
    """先尝试 JSON API，失败后回退到简单索引（国内网络友好）。"""
    errors: list[str] = []

    if args.json_base:
        try:
            return fetch_from_json_api(package, args.json_base, args.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            errors.append(f"JSON API({args.json_base}) 失败: {exc}")

    try:
        return fetch_from_simple_index(package, args.index_url, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
        errors.append(f"简单索引({args.index_url}) 失败: {exc}")

    raise RuntimeError("; ".join(errors))


# --------------------------------------------------------------------------- #
# 版本记录文件读写
# --------------------------------------------------------------------------- #
def empty_record() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "packages": {}}


def load_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_record()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"版本记录文件 {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"版本记录文件 {path} 顶层必须是 JSON 对象")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("updated_at", utc_now())
    if not isinstance(data.get("packages"), dict):
        data["packages"] = {}
    return data


def apply_updates(
    record: dict[str, Any], results: dict[str, dict[str, Any]], verbose: bool
) -> list[str]:
    """把查询结果合并进记录，返回发生变化的包名列表。"""
    changed: list[str] = []
    packages: dict[str, Any] = record["packages"]
    now = utc_now()

    for name, fetched in results.items():
        latest = fetched["latest"]
        entry = packages.get(name) or {}
        current = normalize_version(str(entry.get("latest") or "")) or None

        if not is_newer(latest, current):
            log(f"  = {name}: {current}（已是最新）", verbose_only=True, verbose=verbose)
            entry.setdefault("latest", latest)
            entry.setdefault("first_seen_at", entry.get("first_seen_at") or now)
            entry.setdefault("updated_at", entry.get("updated_at") or now)
            entry.setdefault("history", [])
            packages[name] = entry
            continue

        history = [h for h in entry.get("history") or [] if isinstance(h, dict)]
        history.append({"version": latest, "detected_at": now})
        packages[name] = {
            "latest": latest,
            "previous": current,
            "first_seen_at": entry.get("first_seen_at") or now,
            "updated_at": now,
            "release_time": fetched.get("release_time") or entry.get("release_time"),
            "source": fetched.get("source"),
            "history": history[-50:],
        }
        changed.append(name)
        old = current or "（无记录）"
        log(f"  ↑ {name}: {old} -> {latest}", verbose=verbose)

    if changed:
        record["updated_at"] = now
    record["packages"] = dict(sorted(packages.items()))
    return changed


def write_record(path: Path, record: dict[str, Any]) -> bool:
    """原子写入；内容无变化时返回 False（不触碰文件，避免脏 diff）。"""
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".versions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 akshare / aktools 的 pip 版本，并自动更新本地版本记录文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="版本记录文件路径")
    parser.add_argument(
        "--packages",
        default=",".join(DEFAULT_PACKAGES),
        help="要检查的 PyPI 包名，逗号分隔",
    )
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL, help="PEP 503 简单索引地址（回退源）")
    parser.add_argument(
        "--json-base",
        default=DEFAULT_JSON_BASE,
        help="PyPI JSON API 前缀；传空字符串则只用简单索引",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="单次请求超时（秒）")
    parser.add_argument("--check", action="store_true", help="只检查不写文件；有更新时退出码为 2")
    parser.add_argument("--dry-run", action="store_true", help="只检查并打印结果，不写文件")
    parser.add_argument("--allow-failures", action="store_true", help="单个包查询失败时不算整体失败")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出详细日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    packages = [p.strip() for p in args.packages.split(",") if p.strip()]
    if not packages:
        print("错误：--packages 不能为空", file=sys.stderr)
        return 1

    record = load_record(args.file)
    print(f"版本记录文件: {args.file}", flush=True)
    print(f"检查包: {', '.join(packages)}", flush=True)

    results: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    for name in packages:
        try:
            results[name] = fetch_latest(name, args)
        except Exception as exc:  # noqa: BLE001 - 单个包失败不影响其它包
            failed.append(name)
            print(f"  ! {name}: 查询失败 - {exc}", file=sys.stderr, flush=True)

    changed = apply_updates(record, results, args.verbose)

    write = not (args.check or args.dry_run)
    written = write_record(args.file, record) if write else False

    if changed:
        print(f"检测到新版本: {', '.join(changed)}", flush=True)
    else:
        print("所有包均为最新版本", flush=True)

    if write:
        print(f"版本记录文件已{'更新' if written else '保持不变'}", flush=True)
    elif written is False and (args.check or args.dry_run):
        print("（--check/--dry-run 模式，未写入文件）", flush=True)

    if failed and not args.allow_failures:
        return 1
    if args.check and changed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
