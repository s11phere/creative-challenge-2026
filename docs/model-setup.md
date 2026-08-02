# 模型部署与下载

TEI（Text Embeddings Inference）服务需要加载 embedding 模型。
默认情况下，TEI 容器启动时会自动从 HuggingFace Hub 下载模型。
中国用户或离线环境可通过镜像源预下载模型文件。

## 模型清单

| 服务 | 模型 | HuggingFace ID | 大小 |
|------|------|----------------|------|
| Embedding | Qwen3-Embedding-0.6B | `Qwen/Qwen3-Embedding-0.6B` | ~400 MB |
| Reranker | bge-reranker-v2-m3 | `BAAI/bge-reranker-v2-m3` | ~2.2 GB |

## 方式一：自动下载（默认，推荐）

TEI 容器首次启动时自动从 HuggingFace Hub 下载模型，后续启动使用 Docker 层面缓存。
无需手动操作。embedding 与 reranker 是两个独立 TEI 服务，分别由 `embedding` / `reranker` profile 控制：

```bash
# 只起 embedding
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding up --build --detach --wait

# 两个都起（embedding + reranker）
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding --profile reranker up --build --detach --wait
```

CPU 主机用 `compose.cpu.yaml` 覆盖（同样加两个 profile）。

## 方式二：镜像源加速（中国用户）

通过环境变量 `HF_ENDPOINT` 或 `HUGGINGFACE_HUB_CACHE` 配置镜像源。

### 方案 A：配置 Docker daemon 代理

编辑 `/etc/systemd/system/docker.service.d/proxy.conf`（如文件不存在则创建）：

```
[Service]
Environment="HTTP_PROXY=http://your-proxy:port"
Environment="HTTPS_PROXY=http://your-proxy:port"
Environment="NO_PROXY=localhost,127.0.0.1"
```

重启 Docker：

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
```

### 方案 B：使用镜像站点预下载模型

推荐镜像站：
- 南京大学镜像：`https://mirror.nju.edu.cn/huggingface-models`
- HuggingFace 官方中国镜像：`hf-mirror.com`

预下载命令示例（使用 `huggingface-cli`）：

```bash
# 设置镜像源
export HF_ENDPOINT=https://hf-mirror.com

# 安装 huggingface-cli（如未安装）
pip install huggingface-hub

# 下载模型
huggingface-cli download Qwen/Qwen3-Embedding-0.6B \
  --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
```

下载后的模型会缓存在 `~/.cache/huggingface/` 目录。
TEI 容器可通过 Docker 卷共享该缓存，或通过环境变量 `HF_HOME` / `HUGGINGFACE_HUB_CACHE`
指定缓存路径。

## 方式三：完全离线部署

1. 在有网络的机器上下载模型：

```bash
huggingface-cli download Qwen/Qwen3-Embedding-0.6B \
  --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 \
  --local-dir ./models/qwen3-embedding-0.6b
```

2. 将模型目录传输到离线主机。

3. 创建 `compose.offline.yaml` 覆盖文件，使用本地路径挂载模型：

```yaml
services:
  tei:
    volumes:
      - ./models/qwen3-embedding-0.6b:/models/embedding:ro
    command:
      - --model-id
      - /models/embedding
      - --revision
      - 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
```

4. 使用离线覆盖文件启动：

```bash
docker compose -f deploy/compose.yaml -f compose.offline.yaml \
  --env-file .env --profile embedding up --detach
```

## 验证模型服务

模型服务启动后，检查健康状态：

```bash
curl http://localhost:8080/health
```

预期返回 `OK`。

测试 embedding：

```bash
curl http://localhost:8080/embed \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"inputs": "测试文本", "normalize": true}'
```

### 验证 reranker

reranker 服务默认端口 `8081`：

```bash
curl http://localhost:8081/health
```

预期返回 `OK`。测试 rerank：

```bash
curl http://localhost:8081/rerank \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"query": "决策树", "texts": ["剪枝", "线性回归"]}'
```
