# transcription2：Vast.ai 源码安装

SheetSage2 + MuScriptor 的 Web 服务部署源码。不包含模型、访问凭据、用户音频或生成结果。

## 从 Vast 官方 PyTorch 模板创建

1. 镜像选择 `vastai/pytorch:2.8.0-cuda-12.8.1-py312-24.04-2026-09-08`，启动模式选择 SSH 或 Jupyter + SSH。
2. 保留 On-start Script 中的 `entrypoint.sh`。
3. 添加环境变量：

   ```text
   PROVISIONING_SCRIPT=https://raw.githubusercontent.com/trigsoft/transcription2/v0.1.0/sheetstage2/deploy/provision-vast.sh
   ```

4. 在 Vast 账号的私有环境变量设置中添加 `HF_TOKEN`，对应账号需有 MuScriptor Large 模型访问权限；不要把 Token 保存进公开模板。
5. 磁盘建议从 40GB 起。脚本仅支持 Ubuntu 24.04、x86_64、Python 3.12，要求实际 PyTorch CUDA 运行库至少 12.8 且 GPU 可用。

安装入口把固定版本源码下载到 `${WORKSPACE:-/workspace}/transcription`，复用 `install-vast.sh`。先检查 TorchAudio 实际导入，再安装其他依赖和工具，最后顺序下载模型及资源。只有 TorchAudio 的专用错误码 42 触发系统 Python 的 CUDA 12.8 配套重试；其他错误直接终止。

网站监听 `127.0.0.1:8791`，由 Supervisor 管理。安装成功会写入 `deploy/vast-provisioned`，再次执行入口时不会重复安装。失败不会写入成功标记，修复后可重新执行入口。

## 访问网站

SSH 隧道无需增加公开端口：

```bash
ssh -p <SSH端口> root@<服务器IP> -L 7896:127.0.0.1:8791
```

浏览器访问 `http://localhost:7896`。

如需在 Vast Instance Portal 中点击访问，在模板现有 `PORTAL_CONFIG` 末尾追加：

```text
|localhost:7896:8791:/:Transcription
```

同时在模板中添加 TCP 端口 `7896`（Docker Options 为 `-p 7896:7896`）。7896 是代理入口，8791 是网站内部端口；官方模板原有 6006 用于 TensorBoard。公网实际映射端口由 Vast 分配。

## 日志与验证范围

- 安装入口日志：`/workspace/transcription/deploy/provision.log`（设置 WORKSPACE 时目录随之变化）。
- 安装耗时：同目录的 `install-times.tsv`。
- 网站日志：`/var/log/portal/transcription-sheetsage2.log`。
- 本地验证使用隔离单元测试和 Shell 语法检查；本次发布没有在远端执行安装，也没有执行歌曲转录或端到端测试。

## 上游与许可

- MuScriptor 源码来自 <https://github.com/muscriptor/muscriptor>，保留 `muscriptor/LICENSE` 的 MIT 许可及版权声明。
- SheetSage2、MERT 和 MuScriptor 模型下载自对应的 Hugging Face 仓库，使用时需遵守各模型许可与访问条件。
- 前端 jsmediatags 保留 `sheetstage2/pdf-web/dist/vendor/jsmediatags-LICENSE`。

本仓库发布当前本地部署源码快照，包含本地修改；不是上游项目的原样镜像。
