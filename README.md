# RotoWeave 4.0

RotoWeave is an AI-assisted image and video matting workflow for material cleanup, character packaging, remote inference, and Unity import.

RotoWeave 是一套 AI 辅助图像与视频抠图工作流，支持素材处理、角色打包、远程推理和 Unity 导入。

## Features / 基本功能

- Local Basic matting and optional remote High/Ultra inference. / 本地 Basic 抠图与可选的远程 High/Ultra 推理。
- Image-sequence and video material processing. / 图像序列与视频素材处理。
- Character package export and Unity importer. / 角色包导出与 Unity 导入。
- Separate Client and Server deployment. / Client 与 Server 可独立部署。

## Quick start / 快速开始

Windows x64 is currently supported. The first setup requires an internet connection to download the required runtime and dependencies.

当前支持 Windows x64。首次初始化需要联网下载运行环境和依赖。

### Client / 客户端

```powershell
.\Setup-RotoWeave.cmd Client
.\Start-RotoWeave.cmd Client
```

### Server / 服务端

```powershell
.\Setup-RotoWeave.cmd Server
# Obtain and configure the supported model files from their official upstream pages.
# 从官方上游页面自行获取并配置所需模型。
.\Start-RotoWeave.cmd Server
```

Use `Check-RotoWeave.cmd Client` or `Check-RotoWeave.cmd Server` to verify an installation, and use the matching `Stop-RotoWeave.cmd` command to stop it.

可使用 `Check-RotoWeave.cmd Client` 或 `Check-RotoWeave.cmd Server` 检查安装状态，并使用对应的 `Stop-RotoWeave.cmd` 停止服务。

## Models / 模型

No model weights are stored in this repository or included in GitHub Release assets. Obtain models directly from their official upstream pages and review their terms before use. Any private authorization held by the RotoWeave author is not transferred to users.

本仓库及 GitHub Release 均不包含任何模型权重。请仅从官方上游页面获取模型，并在使用前自行确认其条款。RotoWeave 作者持有的任何私有授权均不会转移给用户。

| Component / 组件 | Official source / 官方地址 |
| --- | --- |
| BiRefNet Lite Matting | https://huggingface.co/ZhengPeng7/BiRefNet_lite-matting |
| SAM2Matting | https://huggingface.co/FudanCVL/SAM2Matting |
| CorridorKey Green | https://huggingface.co/nikopueringer/CorridorKey_v1.0 |
| CorridorKey Blue | https://huggingface.co/nikopueringer/CorridorKeyBlue_1.0 |
| ViTMatte-B | https://drive.google.com/file/d/1mOO5MMU4kwhNX96AlfpwjAoMM4V5w3k-/view?usp=sharing |
| SAM3 | https://huggingface.co/facebook/sam3 |

## Usage terms / 使用权限

Copyright (c) 2026 yetinakooper-prog. Permission is granted to download, run, and modify RotoWeave locally for personal, non-commercial use only. Commercial use, redistribution, resale, sublicensing, and publication of the original or modified software are prohibited without prior written permission. No general open-source license is granted.

版权所有 (c) 2026 yetinakooper-prog。仅允许个人以非商业目的下载、运行和在本机修改 RotoWeave。未经事先书面许可，禁止商业使用、再分发、转售、再许可，以及公开发布原版或修改版软件。本项目不授予通用开源许可。

Open to remote opportunities and custom AI Agent development. For roles or collaboration, open an Issue with the [Collaboration] prefix.

正在寻找远程工作机会，也提供 AI Agent 定制开发；工作或合作请使用 [Collaboration] 前缀提交 Issue。
