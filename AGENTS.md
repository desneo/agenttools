# 项目约定

- 本仓库维护 Codex 全局配置与自定义 skills，使用说明见 [README.md](README.md)。
- `codex/AGENTS.md`：部署给 Codex 使用的全局指令，保持在 60 行以内。
- `codex/config.toml`：带中文注释的全局审批设置；部署时按字段合并，保留目标其他配置。
- `codex/skills/<名称>/SKILL.md`：自定义 skill；按目录增量软链接安装。
- 自己维护的 skill 名称和目录统一使用 `zsh-` 前缀。
- [zsh-codex-sync](codex/skills/zsh-codex-sync/SKILL.md)：本机及 SSH 远程同步部署，辅助代码放在 skill 自身目录。
- 提交信息、脚本日志和项目文档使用中文；代码标识符与命令保持原文。
- 项目指令保持简洁，以索引为主；详细行为与维护说明放入 README 或专门文档。
- 修改部署行为时同步更新对应 skill 和 README；保持重复执行安全，不丢弃本地改动或删除其他来源的 skills。
- 校验方式：`python3 -m unittest discover -s tests -v`；新增或修改 skill 时使用 skill-creator 的 `quick_validate.py` 校验。
- 测试使用临时 HOME 和模拟外部命令，不在测试中真实安装软件、修改用户配置或连接远程机器。
