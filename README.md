# LLM Wiki MCP 验证台

这是一个与 `llm-wiki-web` 完全分离的小型 Web 项目，用来真实验证 LLM Wiki 的两套 Streamable HTTP MCP：

- 管理员 MCP：创建知识库、上传资料、查看解析结果、编译、轮询任务、浏览 Wiki；
- 公开 MCP：检索、读取 Wiki，以及在本项目完成回答后按原规则沉淀问答；
- 验证台自身：保存浏览器侧多轮对话，调用用户临时填写的 OpenAI 兼容模型进行流式回答。

验证台**不访问** LLM Wiki 的 REST API、SQLite 文件或业务源码数据。所有知识库操作均经过 MCP；模型配置仅存于浏览器当前会话，不写进 `.env` 或对话数据库。

## 启动顺序

先启动主项目的两个 MCP。管理员 MCP 具备写入权限，务必只绑定受控本机或内网。

PowerShell（在 `llm-wiki-web` 目录）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\scripts\start-mcp-http.ps1 -AllowRequestModelOverrides
```

新开一个终端：

```powershell
.\scripts\start-admin-mcp-http.ps1 -AllowRequestModelOverrides
```

CMD：

```bat
scripts\start-mcp-http.cmd -AllowRequestModelOverrides
scripts\start-admin-mcp-http.cmd -AllowRequestModelOverrides
```

Linux/macOS：

```sh
sh ../llm-wiki-web/scripts/start-mcp-http.sh --allow-request-model-overrides
sh ../llm-wiki-web/scripts/start-admin-mcp-http.sh --allow-request-model-overrides
```

然后启动本验证项目。

PowerShell：

```powershell
cd D:\llm-wiki-project\llm-wiki-mcp-validator
.\scripts\start.ps1
```

CMD：

```bat
cd /d D:\llm-wiki-project\llm-wiki-mcp-validator
scripts\start.cmd
```

Linux/macOS：

```sh
cd /path/to/llm-wiki-mcp-validator
chmod +x scripts/start.sh
./scripts/start.sh
```

脚本首次运行会创建项目自己的 `.venv` 并安装依赖。启动后打开 <http://127.0.0.1:8044>；可通过 `VALIDATOR_PORT` 或 `.\scripts\start.ps1 -Port 8045` 改端口。

## 使用流程

1. 打开右上角“连接配置”，确认公开 MCP 与管理员 MCP 地址；默认是 `8030` 与 `8031`。
2. 填写对话模型的 Base URL、API Key、模型名称。Base URL 可填写 `/v1` 或完整的 `/v1/chat/completions` 地址；验证台会归一化为 OpenAI SDK 需要的地址。
3. 勾选“将对话模型配置通过 Header 传给 MCP”，点击“测试两个 MCP”。仅当两个 MCP 以 `AllowRequestModelOverrides` 启动时，该勾选才会把模型临时传给编译等管理员任务。
4. 在“知识库管理”页创建或选择知识库，上传资料，查看解析 Markdown，然后点击“编译待处理”。任务日志来自管理员 MCP 的 `get_job_status` 轮询。
5. 在 Wiki 浏览区按类型查看轻量目录，点击某页读取正文和可选表格。
6. 在“多轮对话”页勾选一个或多个知识库。可手动选择 Wiki；不选时会通过公开 MCP 自动检索并捞取最多两跳上级概念。回答流式显示，底部单独列出实际读取的 Wiki。停止会取消验证台本地模型流，并标记该轮已取消；“重新生成”复用原用户问题和原知识库范围。

问答沉淀仅使用本轮实际读取的直接概念/实体页作为证据；没有证据、只有上级背景页或被模型判为低价值的问题都不会写入 query 页面。

## 配置与安全边界

`.env.example` 只配置监听地址、MCP 地址、上传临时目录和超时。复制为 `.env` 后可按需修改：

```env
VALIDATOR_HOST=127.0.0.1
VALIDATOR_PORT=8044
VALIDATOR_PUBLIC_MCP_URL=http://127.0.0.1:8030/mcp
VALIDATOR_ADMIN_MCP_URL=http://127.0.0.1:8031/mcp
VALIDATOR_CHAT_TIMEOUT_SECONDS=600
VALIDATOR_CHAT_MAX_RETRIES=2
```

Embedding 和视觉模型不写在这个验证台的 `.env`：它们由主项目的 MCP 服务端使用，配置在 `../llm-wiki-web/backend/.env`：

```env
# Chroma 混合检索使用；更换模型或维度后需要重建索引
EMBEDDING_URL=https://your-embedding-server/v1
EMBEDDING_API_KEY=your-embedding-key
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_DIMENSION=1024

# 管理员 MCP 导入资料时的图片描述；不启用时保留默认描述
VISION_ENABLED=true
VISION_BASE_URL=https://your-vision-server/v1
VISION_API_KEY=your-vision-key
VISION_MODEL=your-vision-model
```

验证台当前只通过 `X-LLM-Base-URL`、`X-LLM-API-Key`、`X-LLM-Model` 临时切换文本对话模型。Embedding 不能按浏览器会话切换，因为已有 Chroma 向量必须与同一模型和维度匹配；视觉模型也不通过验证台动态传入，而是随管理员 MCP 的导入配置运行。

请不要将管理员 MCP 暴露到公网，也不要在不可信网页、反向代理日志或截图中泄露浏览器填写的 API Key。验证台将它放在 `sessionStorage` 中，因此关闭当前浏览器会话后不再保留；本机有其他不可信用户时仍不建议保存密钥。

浏览器上传文件会先暂存到 `data/uploads/<随机目录>`，管理员 MCP 完成同步导入后立即清理。第一版要求验证台与管理员 MCP 在同一台机器，因为管理员 MCP 接收的是绝对文件路径。

## 验证与测试

不需要模型即可做基础单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真实端到端验收依次验证：两个 MCP 的连接、知识库枚举、资料上传、编译任务轮询、Wiki 浏览、自动检索、流式多轮对话、重新生成、停止与问答沉淀。编译任务需要主项目已经配置可用的模型；只测试浏览与 MCP 协议时不需要填写模型密钥。
