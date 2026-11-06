---
name: zsh-codex-sync
description: 将 agenttools 仓库中的 Codex 全局配置和自定义 skills 同步部署到本机或 SSH 远程用户目录；适用于配置同步、更新和增量软链接部署。
---

# zsh-codex-sync

配置来源是 agenttools 仓库的 `codex/`，远程仓库地址为 `https://github.com/desneo/agenttools.git`。
支持 POSIX 和原生 Windows 目标。部署到目标用户的 `~/.codex/`，若目标环境设置 `CODEX_HOME` 则使用该目录；用户显式指定部署目录时优先使用指定目录。Windows 的 `~` 使用目标用户的 `USERPROFILE`。
同时将 `codex/AGENTS.md` 软链接到目标用户的 `~/.dsh/AGENTS.md`，供 dsh agent 使用。

## 执行

使用本 skill 的 [scripts/sync.py](scripts/sync.py) 完成部署，路径相对于本 skill 目录。
脚本只依赖 Python 3 标准库；远程部署还需要本机 SSH 客户端和远程 Python 3。
使用 `--update` 时若目标缺少 Git，在 Ubuntu/Debian 上通过 apt 自动安装 Git 和 CA 证书；使用 root 或 sudo，`--password` 同时供 sudo 使用。未提供密码时尝试免密 sudo，失败后报告原因。
Windows 目标需预装 Python 3 和 Git for Windows（更新仓库时使用），并启用开发者模式或为执行账户授予创建符号链接权限；脚本修改配置前会预检文件及目录链接。缺少 Git 时报告安装要求，不调用 apt，不自动修改系统权限。

### 本机

默认从本 skill 所在的 agenttools 仓库部署，直接使用当前工作区内容，不主动拉取远端。
如果 skill 被复制到其他位置，使用 `--source` 指定 agenttools 仓库根目录。

```bash
python3 <skill目录>/scripts/sync.py
python3 <skill目录>/scripts/sync.py --source ~/agenttools
```

用户要求从 GitHub 更新到最新版本时增加 `--update`；缺少仓库时该选项通过 HTTPS 克隆。
用户指定自定义部署目录时使用 `--target`。

Windows 本机可在 PowerShell 中执行：

```powershell
py -3 -X utf8 "$env:USERPROFILE/agenttools/codex/skills/zsh-codex-sync/scripts/sync.py"
```

### SSH 远程

根据用户提供的主机、用户名和认证方式执行。缺少必要连接信息时先询问，不猜测主机或用户。
使用默认端口 22，不提供端口参数。远程仓库默认位于登录用户的 `~/agenttools`，不上传本机工作区。

```bash
python3 <skill目录>/scripts/sync.py --host <主机> --user <用户名> --update
python3 <skill目录>/scripts/sync.py --host <主机> --user <用户名> --password '<密码>' --update
# Windows 远程目标，根据其 PATH 使用 python 或 py
python3 <skill目录>/scripts/sync.py --host <主机> --user <用户名> --remote-python python --update
```

`--source`、`--target` 在远程模式下均解释为远程路径，不在本机展开；Windows 可使用 `C:/Users/alice/agenttools` 或带引号的反斜杠路径。`--remote-python` 默认 `python3`，可选 `python` 或 `py`（自动加 `-3`），均须指向 Python 3；启动器使用单行 Base64，适配 POSIX shell、CMD 和 PowerShell，JSON 标准输入传输路径及 helper，统一使用 UTF-8。
Windows SSH 目标需启用 OpenSSH Server，并确保该 SSH 会话可运行 Python、Git 和创建软链接。用户名可包含点号、空格或域前缀，按参数引用。
`--password` 用于 SSH 和安装 Git 时的 sudo，密码通过加密 SSH 标准输入传递到远程，不写入远程文件或日志；未提供时沿用已有 SSH 认证。自动密码认证仅支持 POSIX 发起端；Windows 发起端使用密钥或 ssh-agent，省略 `--password`。
仅补齐 Git 和 CA 证书，不安装 Codex、CMake。已有 Git 时不运行包管理器；普通链接部署无需 sudo。
远程部署使用 GitHub 已发布版本；用户要求部署尚未推送的本机改动时，先明确来源和传输方式。

## 网络回退

更新仓库前先通过当前网络及现有代理设置检查 GitHub；不可达时尝试 HTTP 代理 `http://192.168.1.56:7890`。
代理只用于本次 Git 和 apt 调用，不写入系统、shell 或 Git 全局配置。两条路由均失败时停止，不关闭 TLS 证书校验。

## 部署约定

- `codex/config.toml` 只合并顶层 `approval_policy`、`sandbox_mode`、`approvals_reviewer`，保留目标的其他字段和注释；缺失字段附带来源中的中文注释，插入到首个表之前。
- 修改前备份 `config.toml`，原子写入普通文件；值一致时跳过。已有软链接会备份并转成普通文件，保留其可读内容，原链接指向的文件保持原状。
- 合并兼容 Python 3.10，只处理三个字段的单行字符串写法；发现重复定义、点分键、表写法或未闭合内容时停止，先处理配置。项目或 profile 中的覆盖项保持原状，最终生效值还取决于 Codex 配置优先级。
- `codex/` 根目录的其他普通文件逐个软链接到目标目录，包括 `AGENTS.md`，不替换整个配置目录。
- 本机和 SSH 远程部署均额外创建 `~/.dsh/AGENTS.md -> <仓库>/codex/AGENTS.md`，目录不存在时创建；路径取目标用户的主目录（POSIX 为 HOME，Windows 为 USERPROFILE），不受 `CODEX_HOME` 或 `--target` 影响，沿用相同的备份、跳过和失败还原规则。
- `codex/skills/` 下含 `SKILL.md` 的一级目录逐个软链接到目标 `skills/`，包括本 skill 自身。
- 已经指向同一来源的链接跳过。其他同名文件、目录或失效链接先在旁边备份，再建立新链接。
- 保留其他来源的 skills；不删除仓库已移除 skill 的旧链接，不部署凭据。
- 仓库更新只允许 `origin` 指向本仓库，并通过 HTTPS 快进更新；遇到本地修改、分叉或上游异常时停止，不重置、不清理、不强制覆盖仓库。
- 自动安装和网络回退仍失败，或认证、链接失败时停止，报告原因及已完成步骤，不自动无限重试。

完成后报告目标、配置和 skill 的链接结果、备份位置及失败项。创建软链接不代表已验证 Codex 实际加载；仅在确实检查过时报告发现或加载结果。
