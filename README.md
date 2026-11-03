# aktools 镜像仓库（akise/aktools）

基于上游 [akfamily/aktools](https://github.com/akfamily/aktools) 的 `Dockerfile` 构建并推送 [Docker Hub `akise/aktools`](https://hub.docker.com/r/akise/aktools) 镜像，同时跟踪 `akshare` / `aktools` 的 PyPI 版本变化。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `scripts/check_versions.py` | 检查 akshare、aktools 的 pip 最新版本，发现更新时自动维护版本记录文件 |
| `versions.json` | 版本记录文件（由脚本自动维护，CI 据此判定是否构建镜像） |
| `.github/workflows/docker-publish.yml` | GitHub Actions：版本检查 → 记录文件有变更则拉上游 Dockerfile 构建并推送镜像 |

## 一、版本检查脚本

```bash
# 检查并自动更新 versions.json
python3 scripts/check_versions.py

# 只检查、不写文件（退出码 2 表示有新版本，适合挂到自己的定时任务里做告警）
python3 scripts/check_versions.py --check

# 国内网络：指定镜像源（先试 PyPI JSON API，失败自动回退到简单索引）
python3 scripts/check_versions.py --json-base "" --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 追加/替换要跟踪的包，或指定记录文件位置
python3 scripts/check_versions.py --packages akshare,aktools,pandas --file ./versions.json
```

脚本不依赖第三方库（仅标准库），退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 正常（`--check` 下表示全部已是最新） |
| 1 | 运行异常（网络失败等；可用 `--allow-failures` 放宽） |
| 2 | `--check` 模式下发现可更新的版本 |

`versions.json` 结构：

```jsonc
{
  "schema_version": 1,
  "updated_at": "2026-11-03T04:08:06Z",
  "packages": {
    "akshare": {
      "latest": "1.20.22",
      "previous": "1.20.0",          // 上一次记录的版本
      "first_seen_at": "...",         // 首次入库时间
      "updated_at": "...",            // 最近一次版本变更时间
      "release_time": "...",          // 上游发布时间（PyPI JSON API 提供）
      "source": "https://pypi.org/pypi/akshare/json",
      "history": [{ "version": "1.20.22", "detected_at": "..." }]   // 最多保留 50 条
    }
  }
}
```

写入采用「原子替换 + 内容无变化不落盘」，重复执行不会产生无意义的 git diff。

## 二、CI 自动构建与推送

工作流 `.github/workflows/docker-publish.yml`：

1. **versions**：跑 `scripts/check_versions.py` 刷新 `versions.json`；
   若文件相对上次提交有变化，则提交回写（commit message 以 `chore(versions):` 开头，工作流会自动跳过该提交以免自触发）。
2. **build**：仅当 `versions.json` 有变更（或手动触发时勾选 `force_build`）时执行：
   - `curl` 拉取最新 <https://raw.githubusercontent.com/akfamily/aktools/refs/heads/main/Dockerfile> 作为构建文件（并校验含 `FROM`）；
   - 构建 `linux/amd64` + `linux/arm64` 多架构镜像；
   - 推送到 Docker Hub `akise/aktools`。

触发方式：push 到 `main`/`master`、手动 `workflow_dispatch`、每天 02:00 UTC（10:00 Asia/Shanghai）定时巡检。

### 需要配置的仓库 Secrets / Variables

| 类型 | 名称 | 必填 | 说明 |
| --- | --- | --- | --- |
| Secret | `DOCKERHUB_USERNAME` | ✅ | Docker Hub 用户名（`akise`） |
| Secret | `DOCKERHUB_TOKEN` | ✅ | Docker Hub Access Token（不要用登录密码） |
| Secret | `GH_PAT` | ⬜ | 有仓库写权限的 PAT；不配则用默认 `GITHUB_TOKEN` 回写 |
| Variable | `PYPI_INDEX_URL` | ⬜ | 构建与版本检查用的 pip 源，默认 `https://pypi.org/simple` |
| Variable | `BASE_IMAGE` | ⬜ | 覆盖 Dockerfile 的 `BASE_IMAGE`，默认 `python:3.14-slim-trixie` |

> 国内网络可在 Variables 里设 `PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`、`BASE_IMAGE=docker.m.daocloud.io/library/python:3.14-slim-trixie`。

### 镜像标签

只推送 `latest` 一个标签（每次构建覆盖）：

| 标签 | 含义 |
| --- | --- |
| `latest` | 最近一次构建，始终跟随 akshare / aktools 的最新版本 |

具体版本信息可通过 OCI 标签回溯：`io.akise.akshare.version`、`io.akise.aktools.version`、`org.opencontainers.image.revision`，完整历史见 `versions.json`：

```bash
docker inspect --format '{{json .Config.Labels}}' akise/aktools:latest
```

## 三、本地验证

```bash
# 本地无 docker 时也能验证构建文件可用
curl -fsSL https://raw.githubusercontent.com/akfamily/aktools/refs/heads/main/Dockerfile -o /tmp/Dockerfile.upstream
docker build -f /tmp/Dockerfile.upstream -t akise/aktools:local .
docker run --rm -p 8080:8080 akise/aktools:local   # 打开 http://127.0.0.1:8080/docs
```

## 使用镜像

```bash
docker run -d --name aktools -p 8080:8080 --restart unless-stopped akise/aktools:latest
```

访问 <http://127.0.0.1:8080/docs>；MCP 端点为 `/mcp`。
