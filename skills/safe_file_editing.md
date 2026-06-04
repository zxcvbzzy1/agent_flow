---
name: 安全的文件读写与编辑
description: 用专用 file 工具做干净的文件读写/编辑/补丁，避免 bash heredoc 解析失败
tags: file, 文件, edit, 编辑, 读写, patch, 补丁, read, write, search
---

需要操作文件时，优先用统一的 `file` 工具（而不是把大段内容塞进 bash 命令），按 `operation` 选择：

- `read`：读取文件，可用 `start_line`/`end_line` 只读片段。
- `write`：覆盖写入（自动建父目录）。
- `append`：追加写入。
- `edit`：把 `old_string` 替换为 `new_string`；多处匹配需提供更唯一的串或 `replace_all=true`。
- `apply_patch`：应用 unified diff（git 风格），可一次多文件、按文件原子写入。
- `list_dir` / `glob` / `search_text`：浏览与检索。

要点：
1. 改动已有文件优先 `edit`/`apply_patch`，保持最小 diff，不要整文件重写。
2. 编辑前先 `read` 目标片段，确保 `old_string` 唯一可匹配。
3. 路径可用绝对路径或相对工作目录；批量改动用 `apply_patch` 保证原子性。
4. 只有 `file` 工具无法满足的复杂操作才退回 bash。
