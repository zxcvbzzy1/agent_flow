---
name: excalidraw
description: Generate diagrams using Excalidraw MCP. Use when user asks for diagrams, architecture, workflows, or visual explanations.
---

# Excalidraw Diagram Generator

## When to use
- 用户说：画图 / 架构图 / 流程图 / diagram
- 系统设计 / agent结构 / 网络拓扑

## Instructions

1. 提取节点（components）
2. 提取关系（edges）
3. 构建结构化表示
4. 调用 excalidraw MCP 工具生成图

## Constraints

- 不要输出 ASCII 图
- 必须使用 excalidraw 工具
- 字体改为fontFamily = 5走系统字体
- 文字要指定宽和高
- 不要在 rectangle 里使用 label 属性，请将文字拆分为独立的 text 元素，并使用 containerId 将其绑定到对应的矩形上。