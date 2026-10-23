#!/usr/bin/env python3
"""将 agenttools 的 Codex 配置增量软链接到本机或 SSH 目标。"""

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
    target.mkdir(parents=True, exist_ok=True)
    (target / "skills").mkdir(exist_ok=True)
    log(f"来源：{repository}；目标：{target}")
    for item in configs:
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
