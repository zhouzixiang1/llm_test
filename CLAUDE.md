# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Agnes — 自动化多模态内容生成流水线。使用 Agnes AI API 将文本→图片→视频串联生成，并提供 Web 仪表盘实时监控和控制。前端和所有用户面向的消息均为中文。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 应用（从项目根目录，必须使用 8010 端口）
uvicorn web.app:app --host 0.0.0.0 --port 8010

# 开发模式（自动重载）
uvicorn web.app:app --host 0.0.0.0 --port 8010 --reload

# CLI 工具
python agnes_text.py "你的问题"
python agnes_image.py --prompt "描述" -o output.png
python agnes_video.py --prompt "描述" -o output.mp4
```

本项目无测试框架、无构建系统、无 CI/CD 配置。

## 架构

系统分四层，每层对应一个清晰的职责边界：

### 1. API 客户端层 — `agnes_client.py`

统一的 Agnes AI API 客户端库，提供所有外部 API 调用的封装：
- `chat()` / `chat_simple()` — 文本生成（`agnes-2.0-flash`）
- `generate_image()` — 图片生成（`agnes-image-2.1-flash`）
- `create_video_task()` + `poll_video_result()` + `download_video()` — 异步视频生成（`agnes-video-v2.0`）
- `_api_request()` — 带指数退避的重试包装器（5次重试）
- `parse_json_object()` — 从 LLM 输出中提取 JSON
- API Key 通过 `AGNES_API_KEY` 环境变量配置，`DEFAULT_API_KEY` 为硬编码默认值

### 2. CLI 层 — `agnes_text.py`, `agnes_image.py`, `agnes_video.py`

`argparse` 驱动的薄封装，各自提供 `main()` 入口。

### 3. Web 应用层 — `web/`

| 文件 | 职责 |
|------|------|
| `app.py` | FastAPI 入口：REST 端点、SSE 实时推送（`/api/events`）、静态文件服务、优雅关闭（SIGINT/SIGTERM） |
| `db.py` | SQLite 数据层：`items`（流水线条目）、`settings`（键值配置）、`failure_logs`（失败归档）。线程安全（全局锁） |
| `pipeline.py` | 核心编排引擎：`EventBus`（发布/订阅 SSE 广播）+ `PipelineController`（后台线程循环） |

流水线每轮循环：创建 item → LLM 生成图片提示词 → 调图片 API → LLM 生成视频提示词 → 提交视频任务 → 轮询完成 → 下载视频。支持启动时恢复未完成 item、当前 item 完成后优雅停止、基于主题的提示词生成。

### 4. 前端 — `web/static/`

纯 JS/HTML/CSS 单页应用。通过 SSE 连接 `/api/events` 接收实时更新，REST API 控制流水线。

### 数据流

```
Web UI 设置主题 → SQLite settings 表 → PipelineController 后台线程读取主题
→ LLM 生成图片提示词（避开近期标题）→ 图片 API 生成图片
→ LLM 生成视频提示词 → 视频 API 异步生成 → 下载到磁盘
→ SSE 事件推送实时状态到浏览器
```

## 关键约定

- **Web 服务端口固定为 8010，禁止使用 8000 端口**（8000 已被其他服务占用）
- 数据库文件位于 `web/data/app.db`，生成媒体位于 `web/data/outputs/`
- API 端点前缀：`/api/`；媒体文件通过 `/media/` 提供静态服务
- 视频生成是异步的：先 `create_video_task` 获取 video_id，再轮询 `poll_video_result` 直至完成
- **视频 URL 字段名**：Agnes API 在视频 `completed` 时，下载 URL 存放于 `remixed_from_video_id` 字段（尽管名字有误导性），详见 [API 文档](https://agnes-ai.com/doc/agnes-video-v20)
- `agnes_client.py` 中 `_prefer_ipv4()` 使用 `urllib3` 的 `allowed_gai_family` 强制 IPv4 连接
- 归档操作使用 `db.archive_item_atomic` 保证事务原子性（单事务完成 archive + delete，事务后再删文件）
- `_api_request` 有全局超时上限 `max_total_time`（默认 600s），`Session` 跨重试复用
- `on_progress` 回调在 shutdown 期间跳过写入，防止 `video_progress=100` 与状态不一致
