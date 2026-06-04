---
name: 静态网页部署上线
description: 把生成好的静态网站真正部署上线并在前端预览、可一键关闭/重启
tags: deploy, 部署, web, 网页, frontend, html, 上线, 预览
---

当用户要求“部署/上线/运行起来看效果”一个静态站点时：

1. 先用文件工具把要部署的文件（index.html、css、js、assets 等）真实写入工作目录的某个子目录，例如 `dist/` 或 `site/`。
2. 确认入口文件存在（默认 `index.html`）。
3. 产出 deploy 产物（kind=static）：`source_dir` 指向该静态目录（相对工作目录），`entry` 默认 `index.html`。
   - native ReACT agent：调用 `deploy` 工具，kind=static。
   - coding agent：在最终回复里输出 deploy 标记块。
4. 不要用 web 预览冒充部署——只有 deploy 才会真正开端口并生成可关闭/重启的部署卡片。
5. 部署成功后，卡片会展示实时预览地址；端口被空闲回收或手动关闭后，用户可在卡片上一键“部署”重新拉起。

要点：路径不要越界；静态资源用相对路径引用，确保托管目录自洽。
