#!/usr/bin/env python3
"""合并 Codex 审批配置，并增量软链接全局指令及 skills。"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


REPO_URL = "https://github.com/desneo/agenttools.git"
FALLBACK_PROXY = "http://192.168.1.56:7890"
ALLOWED_ORIGINS = {
    REPO_URL, REPO_URL[:-4],
    "git@github.com:desneo/agenttools.git",
    "ssh://git@github.com/desneo/agenttools.git",
}
POLICY_KEYS = {"approval_policy", "sandbox_mode", "approvals_reviewer"}


def toml_statements(text):
    """按完整语句切分，跳过字符串、注释和数组内的换行，保留原文。"""
    start = index = 0
    quote = None
    brackets = []
    while index < len(text):
        char = text[index]
        if quote:
            if quote.startswith('"') and char == "\\":
                index += 2
                continue
            if text.startswith(quote, index):
                length = len(quote)
                if length == 3:
                    while index + length < len(text) and text[index + length] == char and length < 5:
                        length += 1
                index += length
                quote = None
                continue
            if char == "\n" and len(quote) == 1:
                raise ValueError("config.toml 的单行字符串未闭合")
        elif char in "\"'":
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            continue
        elif char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif char in "[{":
            brackets.append(char)
        elif char in "]}":
            if not brackets or brackets.pop() != {']': '[', '}': '{'}[char]:
                raise ValueError("config.toml 括号不匹配")
        if char == "\n" and quote is None and not brackets:
            yield start, index + 1, text[start:index + 1]
            start = index + 1
        index += 1
    if quote or brackets:
        raise ValueError("config.toml 存在未闭合的字符串或数组")
    if start < len(text):
        yield start, len(text), text[start:]


def policy_entries(text):
    """只识别受管理的顶层字符串，复杂或重复写法停止，其他语句原样保留。"""
    entries = {}
    root = True
    first_table = len(text)
    key_pattern = r'''\s*("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|[A-Za-z0-9_-]+)\s*([=.\]])'''
    for start, end, statement in toml_statements(text):
        stripped = statement.lstrip()
        table = stripped.startswith("[")
        if table:
            first_table = min(first_table, start)
            root = False
        if not root and not table:
            continue
        match = re.match(key_pattern, stripped.lstrip("[") if table else statement)
        if not match:
            continue
        raw_key, operator = match.groups()
        key = json.loads(raw_key) if raw_key.startswith('"') else raw_key.strip("'")
        if key not in POLICY_KEYS:
            continue
        if table or operator != "=":
            raise ValueError(f"{key} 使用了表或点分写法，请先改成顶层字符串")
        value = re.fullmatch(r'''([ \t]*)("(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*')([ \t]*(?:#[^\r\n]*)?(?:\r?\n)?)''', statement[match.end():])
        if not value or key in entries:
            raise ValueError(f"{key} 不是单行字符串或重复定义，请先检查配置")
        raw_value = value.group(2)
        decoded = json.loads(raw_value) if raw_value.startswith('"') else raw_value[1:-1]
        value_start = start + match.end() + len(value.group(1))
        entries[key] = (decoded, value_start, value_start + len(raw_value), start)
    return entries, first_table


def prepare_config(source, destination):
    template = source.read_text(encoding="utf-8")
    desired, _ = policy_entries(template)
    if set(desired) != POLICY_KEYS:
        raise ValueError("来源 config.toml 必须包含三个顶层审批字符串字段")
    if destination.exists() and not destination.is_file():
        raise ValueError(f"目标 config.toml 不是文件：{destination}")
    # 读取有效软链接的内容，但后续原子替换链接本身，避免修改其指向的文件。
    original = destination.read_bytes().decode("utf-8") if destination.exists() else ""
    current, insertion = policy_entries(original)
    changes = []
    missing = []
    for key, (value, _, _, source_start) in desired.items():
        if key in current:
            old, start, end, _ = current[key]
            if old != value:
                changes.append((start, end, json.dumps(value, ensure_ascii=False)))
        else:
            comments = []
            for line in reversed(template[:source_start].splitlines()):
                if not line.lstrip().startswith("#"):
                    break
                comments.insert(0, line)
            missing.append("\n".join(comments + [f"{key} = {json.dumps(value, ensure_ascii=False)}"]))
    if missing:
        prefix = "\n" if insertion and not original[:insertion].endswith("\n") else ""
        changes.append((insertion, insertion, prefix + "\n\n".join(missing) + "\n\n"))
    merged = original
    for start, end, value in sorted(changes, reverse=True):
        merged = merged[:start] + value + merged[end:]
    if merged == original and not destination.is_symlink():
        return None
    return merged


def write_config(destination, content):
    if content is None:
        log(f"审批配置已一致：{destination}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
        if destination.exists():
            os.chmod(temporary, destination.stat().st_mode & 0o777)
        if os.path.lexists(destination):
            suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
            backup = destination.with_name(f"{destination.name}.backup.{suffix}.{os.getpid()}")
            shutil.copy2(destination, backup, follow_symlinks=False)
            log(f"已备份：{backup}")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    log(f"已合并三个顶层审批字段：{destination}")


def log(message):
    print(f"[同步] {message}", flush=True)


def proxy_environment():
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env[key] = FALLBACK_PROXY
    env["no_proxy"] = env["NO_PROXY"] = ""
    return env


def probe_network(env):
    # 使用本次调用的代理配置，不修改用户文件、Git 配置或进程全局环境。
    proxies = urllib.request.getproxies_environment()
    for scheme in ("http", "https"):
        value = env.get(scheme + "_proxy") or env.get(scheme.upper() + "_PROXY")
        if value:
            proxies[scheme] = value
        else:
            proxies.pop(scheme, None)
    # 显式代理已由调用方选定，避免当前进程的 NO_PROXY 覆盖回退路由。
    request = urllib.request.Request(
        "https://github.com", headers={"User-Agent": "zsh-codex-sync", "Host": "github.com"},
    )
    if proxies.get("https"):
        proxy = urllib.parse.urlparse(proxies["https"])
        request.set_proxy(proxy.netloc, proxy.scheme)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        if response.status != 200:
            raise OSError(f"GitHub 返回 HTTP {response.status}")


def network_environment():
    env = dict(os.environ)
    try:
        probe_network(env)
        return env
    except (OSError, urllib.error.URLError):
        log(f"当前网络无法访问 GitHub，尝试代理 {FALLBACK_PROXY}")
    env = proxy_environment()
    try:
        probe_network(env)
    except (OSError, urllib.error.URLError) as error:
        raise ValueError(f"当前网络和备用代理均无法访问 GitHub：{error}") from error
    log("备用代理连接成功，仅对本次部署生效")
    return env


def install_git(password, env):
    if shutil.which("git"):
        return
    if not shutil.which("apt-get"):
        raise ValueError("缺少 Git，当前仅支持在 Ubuntu/Debian 上自动安装")
    prefix = []
    password_input = None
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise ValueError("安装 Git 需要 root 或 sudo 权限，当前缺少 sudo")
        prefix = ["sudo", "-S", "-p", "", "--"] if password is not None else ["sudo", "-n", "--"]
        password_input = password + "\n" if password is not None else None
    proxy_args = []
    for scheme in ("http", "https"):
        proxy = env.get(scheme + "_proxy") or env.get(scheme.upper() + "_PROXY")
        if proxy:
            proxy_args += ["-o", f"Acquire::{scheme}::Proxy={proxy}"]
    apt = ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
           "-o", "Acquire::Retries=0", "-o", "Acquire::http::Timeout=20",
           "-o", "Acquire::https::Timeout=20", *proxy_args]
    log("目标机器缺少 Git，自动安装 Git 和 CA 证书")
    for action in (["-o", "APT::Update::Error-Mode=any", "update"],
                   ["install", "-y", "--no-install-recommends", "git", "ca-certificates"]):
        subprocess.run(prefix + apt + action, input=password_input, text=True, check=True, env=env)
    if not shutil.which("git"):
        raise ValueError("安装结束后仍未找到 Git，请检查目标 PATH")


def git(repository, *args, capture=False, env=None):
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        env=dict(env if env is not None else os.environ, GIT_TERMINAL_PROMPT="0"),
    ).stdout


def update_repository(repository, password=None):
    env = None
    if not shutil.which("git"):
        env = network_environment()
        install_git(password, env)
    if not os.path.lexists(repository):
        env = env if env is not None else network_environment()
        log(f"通过 HTTPS 克隆到 {repository}")
        repository.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--", REPO_URL, str(repository)], check=True,
            env=dict(env, GIT_TERMINAL_PROMPT="0"),
        )
        return
    if not (repository / ".git").exists():
        raise ValueError(f"目标路径已存在，但不是 Git 仓库：{repository}")
    if Path(git(repository, "rev-parse", "--show-toplevel", capture=True).strip()).resolve() != repository:
        raise ValueError("来源路径不是仓库根目录")
    if git(repository, "remote", "get-url", "origin", capture=True).strip() not in ALLOWED_ORIGINS:
        raise ValueError("来源仓库的 origin 不是 agenttools")
    if git(repository, "status", "--porcelain", "--untracked-files=all", capture=True).strip():
        raise ValueError("来源仓库存在本地修改或未跟踪文件，请处理后重试")
    branch = git(repository, "symbolic-ref", "--quiet", "--short", "HEAD", capture=True).strip()
    git(repository, "rev-parse", "--abbrev-ref", "@{upstream}", capture=True)
    if git(repository, "config", "--get", f"branch.{branch}.remote", capture=True).strip() != "origin":
        raise ValueError("当前分支的上游不属于 origin")
    env = env if env is not None else network_environment()
    git(repository, "remote", "set-url", "origin", REPO_URL)
    log(f"快进更新 {repository}")
    git(repository, "pull", "--ff-only", env=env)


def link_item(source, destination):
    if destination.is_symlink():
        try:
            matches = destination.resolve() == source.resolve()
        except (OSError, RuntimeError):
            matches = False
        if matches:
            log(f"链接已就绪：{destination}")
            return
    backup = None
    if os.path.lexists(destination):
        suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
        backup = destination.with_name(f"{destination.name}.backup.{suffix}.{os.getpid()}")
        while os.path.lexists(backup):
            backup = backup.with_name(backup.name + ".1")
        destination.rename(backup)
        log(f"已备份：{backup}")
    try:
        destination.symlink_to(source, target_is_directory=source.is_dir())
    except OSError:
        if backup is not None and not os.path.lexists(destination):
            backup.rename(destination)
            log(f"链接失败，已还原：{destination}")
        raise
    log(f"已链接：{destination} -> {source}")


def deploy(repository, target=None, update=False, password=None):
    repository = Path(repository).expanduser().resolve()
    target = Path(target or os.environ.get("CODEX_HOME") or "~/.codex").expanduser().resolve()
    source = (repository / "codex").resolve()
    # 防止将来源放入目标目录或反向嵌套后产生自链接、破坏来源。
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("来源 codex 目录与部署目录不能相同或互相包含")
    if update:
        update_repository(repository, password)
    if not (source / "AGENTS.md").is_file():
        raise ValueError(f"来源缺少 codex/AGENTS.md：{repository}")
    skill_target = (target / "skills").resolve()
    if skill_target == source or source in skill_target.parents or skill_target in source.parents:
        raise ValueError("目标 skills 目录指向来源目录，请先调整该目录链接")
    configs = sorted(item for item in source.iterdir() if item.is_file())
    skills = source / "skills"
    skill_items = sorted(
        item for item in skills.iterdir() if item.is_dir() and (item / "SKILL.md").is_file()
    ) if skills.is_dir() else []
    for item in configs + skill_items:
        if item.is_symlink():
            raise ValueError(f"来源包含软链接，请先确认实际内容：{item}")
    config_source = source / "config.toml"
    config_content = prepare_config(config_source, target / "config.toml") if config_source.is_file() else None
    target.mkdir(parents=True, exist_ok=True)
    (target / "skills").mkdir(exist_ok=True)
    log(f"来源：{repository}；目标：{target}")
    for item in configs:
        if item.name == "config.toml":
            write_config(target / item.name, config_content)
        else:
            link_item(item, target / item.name)
    for item in skill_items:
        link_item(item, target / "skills" / item.name)
    log(f"部署完成：{len(configs)} 个配置文件，{len(skill_items)} 个 skill")


def deploy_remote(args):
    if not shutil.which("ssh"):
        raise ValueError("本机缺少 SSH 客户端")
    # 通过 SSH 标准输入传输 helper，目标仓库无需预装此 skill。
    bootstrap = (
        "import json,sys\np=json.load(sys.stdin)\nn={'__name__':'codex_sync_remote'}\n"
        "try:\n"
        " exec(compile(p['helper'],'zsh-codex-sync-remote','exec'),n)\n"
        " n['deploy'](p['source'] or '~/agenttools',p['target'],p['update'],p['password'])\n"
        "except Exception as error:\n"
        " print('[错误] 远程部署失败：'+str(error),file=sys.stderr)\n sys.exit(1)\n"
    )
    command = [
        "ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
        "-l", args.user, args.host, "python3 -c " + shlex.quote(bootstrap),
    ]
    payload = json.dumps({
        "helper": Path(__file__).read_text(), "source": args.source,
        "target": args.target, "update": args.update, "password": args.password,
    })
    log(f"SSH 部署：{args.user}@{args.host}")
    if args.password is None:
        subprocess.run(command, input=payload, text=True, check=True)
        return
    with tempfile.TemporaryDirectory(prefix="zsh-codex-sync-") as temporary:
        folder = Path(temporary)
        password_file = folder / "password"
        password_file.touch(mode=0o600)
        password_file.write_text(args.password + "\n")
        askpass = folder / "askpass"
        askpass.write_text('#!/bin/sh\ncat -- "$CODEX_SYNC_PASSWORD_FILE"\n')
        askpass.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=os.environ.get("DISPLAY") or ":0", CODEX_SYNC_PASSWORD_FILE=str(password_file))
        subprocess.run(
            command[:1] + ["-o", "NumberOfPasswordPrompts=1"] + command[1:],
            input=payload, text=True, check=True, env=env, start_new_session=True,
        )


def main():
    parser = argparse.ArgumentParser(description="同步部署 agenttools 的 Codex 全局配置与 skills")
    parser.add_argument("--source", help="agenttools 仓库根目录；SSH 模式下为远程路径")
    parser.add_argument("--target", help="部署目录；默认 CODEX_HOME 或 ~/.codex")
    parser.add_argument("--update", action="store_true", help="通过 HTTPS 克隆或快进更新来源仓库")
    parser.add_argument("--host", help="SSH 主机，默认端口 22")
    parser.add_argument("--user", help="SSH 用户名")
    parser.add_argument("--password", help="SSH 和安装 Git 时的 sudo 密码；本机仅用于 sudo")
    args = parser.parse_args()
    if (args.host is None) != (args.user is None):
        parser.error("--host 和 --user 必须同时指定")
    if args.host is not None and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:%_-]*", args.host):
        parser.error("SSH 主机名或 IP 格式无效")
    if args.user is not None and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*\$?", args.user):
        parser.error("SSH 用户名格式无效")
    if args.password is not None and ("\n" in args.password or "\r" in args.password):
        parser.error("密码不能包含换行符")
    try:
        if args.host:
            deploy_remote(args)
        else:
            deploy(args.source or Path(__file__).resolve().parents[4], args.target, args.update, args.password)
    except subprocess.CalledProcessError as error:
        print(f"[错误] 外部命令执行失败，退出码 {error.returncode}；请查看上方原因", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as error:
        print(f"[错误] 同步部署失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
