# agenttools

个人维护的 Codex 全局配置与自定义 skills 仓库。

## 同步部署 skill

[codex-sync](codex/skills/codex-sync/SKILL.md) 将仓库中的全局配置和自定义 skills 部署到本机或 SSH 远程机器。它按文件、skill 目录增量建立软链接，保留其他来源的内容，同名冲突先备份。

### 首次接入本机

目标机器需已有 Codex 和 Python 3。以下首次下载命令需要本机已有 Git；helper 执行 `--update` 时会自动补齐缺失的 Git。以希望持有配置的用户执行：

```bash
git clone https://github.com/desneo/agenttools.git ~/agenttools
python3 ~/agenttools/codex/skills/codex-sync/scripts/sync.py
```

如果已有仓库，直接运行第二条命令即可。首次部署会把 `codex-sync` 自身也软链接到用户 skills 目录。之后可以在 Codex 中使用：

```text
使用 $codex-sync 将当前仓库的 Codex 配置和 skills 同步到本机。
使用 $codex-sync 从 GitHub 更新仓库并部署到本机。
使用 $codex-sync 将最新配置部署到远程机器，主机是 192.168.1.10，用户名是 alice。
```

可通过 `/skills` 检查当前 Codex 的发现结果；如未刷新，可重启会话。脚本成功创建链接不等于已经验证 Codex 加载了配置和 skills。

### SSH 远程部署

本机需要 Python 3 和 SSH 客户端，远程需要 Python 3。部署使用登录用户目录；`--update` 遇到缺失的 Git 时，在 Ubuntu/Debian 上使用 apt 自动安装 Git 和 CA 证书，需要 root 或 sudo 权限。已有 Git 时不安装软件，普通链接部署无需 sudo。不安装 Codex 或 CMake。

```bash
# 使用已有 SSH 密钥或交互认证，克隆或更新远程 ~/agenttools 后部署
python3 codex/skills/codex-sync/scripts/sync.py \
  --host 192.168.1.10 --user alice --update

# 密码认证，同时供自动安装 Git 时的 sudo 使用
python3 codex/skills/codex-sync/scripts/sync.py \
  --host 192.168.1.10 --user alice --password '密码' --update
```

SSH 默认端口 22，不提供端口参数；现有 SSH 客户端配置仍生效。首次连接接受并记录新主机密钥，已记录密钥发生变化时拒绝连接。临时密码文件仅当前用户可读，退出时清理；命令行密码可能出现在 shell 历史或进程参数中。密码不支持换行符。

远程部署同步的是 GitHub 已发布的仓库内容，不上传本机未提交、未推送的改动。helper 通过 SSH 标准输入传输，因此远程无需预先安装本 skill。

### 辅助命令参数

| 参数 | 行为 |
| --- | --- |
| `--source 路径` | agenttools 仓库根目录；本机默认推导当前 skill 所在仓库，远程默认 `~/agenttools` |
| `--target 路径` | 部署目录，默认目标环境的 `CODEX_HOME` 或 `~/.codex` |
| `--update` | 通过 HTTPS 克隆或快进更新来源仓库；不指定时直接部署已有文件 |
| `--host 主机`、`--user 用户名` | SSH 远程部署，必须同时指定 |
| `--password 密码` | SSH 和自动安装 Git 时的 sudo 密码；本机只用于 sudo |

远程模式下 `--source` 和 `--target` 均为远程路径，不转发本机的 `CODEX_HOME`。

更新前先使用当前网络和现有代理设置检查 GitHub；不可达时尝试 HTTP 代理 `http://192.168.1.56:7890`，用于本次 Git 和 apt 调用。不会写入系统代理、shell 启动文件或 Git 全局配置，始终校验 HTTPS 证书。两条路由都失败时停止。安装 Git 时未提供密码会尝试免密 sudo；需要密码时使用 `--password`。远程 sudo 密码通过加密 SSH 标准输入传递，不写入远程文件。

`--update` 只接受本仓库的 origin，并将其规范为 HTTPS，执行 `git pull --ff-only`。有本地修改、未跟踪文件、分叉、detached HEAD 或上游异常时停止，不丢弃已有内容。依赖、网络或部署失败返回非零退出码，保留已完成步骤；处理原因后可重试。

## 配置与自定义 skills

```text
agenttools/
├── AGENTS.md
├── README.md
├── codex/
│   ├── AGENTS.md                 # 全局指令
│   └── skills/
│       └── codex-sync/
│           ├── SKILL.md          # 同步部署工作流
│           └── scripts/sync.py   # 可靠执行软链接和 SSH 部署
└── tests/test_codex_sync.py
```

默认链接关系：

```text
~/.codex/AGENTS.md             -> <仓库>/codex/AGENTS.md
~/.codex/skills/codex-sync     -> <仓库>/codex/skills/codex-sync
~/.codex/skills/<自定义名称>   -> <仓库>/codex/skills/<自定义名称>
```

`codex/` 根目录的普通文件逐个部署；skills 只安装包含 `SKILL.md` 的一级目录。已指向同一来源的链接跳过，同名文件、目录、失效链接先备份为旁边的 `.backup.<时间>.<进程号>` 路径，链接失败时还原当前项。其他已完成项保留。其他来源的 skills 不删除，来源已移除 skill 的旧链接需自行清理。

新增 skill 时创建 `codex/skills/<名称>/SKILL.md`，包含 `name`、`description` 的 YAML frontmatter 和指令，按需添加辅助资源，再运行 `$codex-sync`。本机可直接部署工作区；远程部署前需将变更发布到仓库。

按本仓库约定安装到 `~/.codex/skills/`。官方文档当前列出的用户 skills 标准路径为 `~/.agents/skills/`，使用时需检查当前 Codex 版本对约定目录的发现结果。全局指令 `codex/AGENTS.md` 与项目根目录 `AGENTS.md` 分别维护，不把凭据加入配置仓库。

## 开发校验

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时目录、本地 Git 仓库及模拟 SSH，不改动真实用户配置、不安装软件、不访问外网。skill 格式使用 `skill-creator` 自带的 `quick_validate.py` 校验。

参考：[Codex 全局指令](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Skills](https://learn.chatgpt.com/docs/build-skills)。
