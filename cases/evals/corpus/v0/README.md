# Corpus v0

`manifest.yaml` 是评测语料的权威允许列表。所有检索、切片、索引操作必须以此文件为唯一入口，
不得扫描 `cases/` 下的其他目录或文件。

## 路径约定

所有 `path` 值相对于 `cases/` 根目录。`content_sha256` 为文件原始字节的 SHA-256。

## 使用许可

每个 source 的 `allowed_uses` 字段明确规定了允许的用途。
`sensitivity` 和 `redistribution` 策略同样适用于解析后的文本、摘录、嵌入向量和评测报告。

## 当前状态

本语料库已按 `docs/stage-0-acceptance.md` 冻结为 `internal_team_only`。PDF 的固定提取协议、
替代提取器对比和公式视觉转录分别见 `PDF-EXTRACTION.md`、`PDF-EXTRACTOR-COMPARISON.md` 和
`PDF-VISUAL-REVIEW.yaml`。原始语料、派生内容和 `cases.jsonl` 仍不得提交到 Git 或发送到
外部服务；内部冻结不等于公开再分发授权。
