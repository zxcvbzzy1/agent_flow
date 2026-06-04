---
name: 后端服务部署
description: 用常驻命令(uvicorn/flask/node 等)把后端服务真正跑起来并分配端口预览
tags: deploy, 部署, backend, 后端, uvicorn, flask, fastapi, node, server, 服务, 端口
---

当用户要求把一个后端服务“部署/跑起来”时：

1. 先用文件工具把后端源码（如 `app.py`、`requirements`、入口文件）真实写入工作目录。
2. 产出 deploy 产物（kind=command）：
   - `source_dir` 指向代码目录（相对工作目录）。
   - `command` 是常驻启动命令，并且**必须用 `$PORT` 占位**监听端口，例如：
     `uvicorn app:app --host 127.0.0.1 --port $PORT`
     `node server.js`（在代码里读 `process.env.PORT`）
   - 需要环境变量时通过 `env` 传入。
3. 系统会把 `$PORT` 替换为分配到的端口并在 127.0.0.1 起进程，就绪后卡片展示预览地址。
4. 启动失败会显示 failed 与日志尾部；修正命令/依赖后可在卡片上一键重新部署。

要点：监听 127.0.0.1 而不是 0.0.0.0 之外的地址；端口务必用 `$PORT`，否则重启时会端口冲突。
