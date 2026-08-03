# 阶段 3 重新开启尝试：knowledge-qa-v1 development

> 记录日期：2026-08-03
>
> 结论：development 工程复核完成；阶段 3 正式质量前置门未通过，未冻结配置，未运行 holdout。
>
> 本记录承接 [ADR-010](adr/010-stage-3-termination-and-evaluation-boundary.md)，不改写原阶段 3
> 终止结论，也不把本次 provisional 指标当作正式质量基线。

## 1. 评测边界

- dataset：`knowledge-qa-v1`，cases SHA-256
  `a16a953cff715b4de95ade05d5b8aa173ddbd4fd28c380d54eb7dc2014270c8`。
- corpus：继续使用冻结 `corpus/v0`，manifest SHA-256
  `53d6f863060dd7d5e6affb0abda64348f0b498995b3d1cda5ef80f0e576ca738`，90 个来源、12 个 Space。
- split：development 127 条、holdout 149 条；仅执行 development。协议排除 16 条依赖 P1
  code/notebook 格式的 development case 后，检索指标分母为 111 条。
- config：`retrieval-v1-knowledge-qa-v1.yaml`，config hash
  `4b4435f4f2251acc2adc638cc64a0d253385937f83509d2dd3493dabaa59ea54`，仍为 `provisional`，
  `formal_runs_enabled=false`。
- 评测器：当前 runner 的 evidence 指标是 strict locator diagnostic；v1 的
  `acceptable_evidence_sets` 尚未被 runner 用作 claim-aware 正式评分。

v1 的 dataset validation 和 config `--validate-only` 均通过，source hash、version、locator、
quote、excerpt hash、Space 隔离和 split 结构没有发现校验问题。v1 主要修复 development 标注并
保留 v0 corpus/split，因此尚不能视为已完成“代表性 materially improved”的新评测边界。

## 2. 本地 GPU 运行环境

使用已运行的隔离 GPU TEI 服务：

| 能力 | 固定身份 |
| --- | --- |
| Embedding | Qwen3-Embedding-0.6B，revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`，768 维，`qwen3-knowledge-qa-v1` 指令版本 |
| Reranker | `BAAI/bge-reranker-v2-m3`，TEI CUDA 镜像 |
| Embedding identity | `embedding-v1-14f6b9c9cc13fd0100a2ddf9` |
| Database | 独立 `evaluation_gpu` PostgreSQL；通过 `EVALUATION_DATABASE_ISOLATED=1` 运行 |

未调用外部 Chat/Embedding/Provider。原始机器报告写入被 Git 忽略的
`tmp/stage1-knowledge-qa-v1-development.json`，不提交逐 case 问题、引用或正文。

## 3. Development 消融结果

指标只报告聚合值；`P0 cases=111`，失败率为基础设施/执行失败率，不等同于检索质量通过率。

| 实验 | Claim Recall@10 | Evidence Recall@10 | MRR | Evidence nDCG@10 | Full Evidence | P50 | P95 | Failure rate | Must-exclude |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Keyword | 16.3488% | 12.1339% | 0.2772 | 0.1630 | 7.9208% | 10.7 ms | 71.6 ms | 0% | 0 |
| Dense exact | 70.0272% | 61.9247% | 0.6701 | 0.5035 | 52.4752% | 167.8 ms | 201.0 ms | 0% | 0 |
| Dense IVFFlat | 62.1253% | 56.9038% | 0.6383 | 0.4655 | 43.5644% | 159.8 ms | 184.6 ms | 0% | 0 |
| Hybrid | 69.2098% | 61.5063% | 0.5689 | 0.4532 | 51.4851% | 173.7 ms | 212.9 ms | 0% | 0 |
| Hybrid + Reranker | 69.7548% | 62.3431% | 0.6839 | 0.5239 | 52.4752% | 274.7 ms | 382.6 ms | 0% | 0 |

## 4. 结论和后续门

1. 本次结果证明 v1 development 可复现，且 GPU 模型、隔离数据库、索引和失败分类正常工作。
2. 结果不能关闭阶段 3：当前指标低于阶段 0 的正式检索门槛，且 v1 没有解决 ADR-010 指出的
   评测集代表性问题；strict locator 也不是 claim-aware 正式分数。
3. 不得把本次 config 标记为 `frozen`，不得设置 `formal_runs_enabled=true`，不得执行或读取
   holdout，不得拼接旧报告形成正式结果。
4. 下一次正式质量尝试必须发布新的 dataset/config version，明确新增覆盖或代表性来源、标注/locator
   审查和 claim-aware evaluator 方案，然后从 development 重新开始。根据 ADR-011，当前工作可以
   进入阶段 4/5 provisional 工程，但阶段 4 正式 QA 配置冻结和正式 answer holdout 仍然阻塞。

## 5. 实际命令

```powershell
.venv\Scripts\python.exe cases/scripts/validate.py --dataset-dir cases/evals/datasets/knowledge-qa-v1 --max-issues 100
.venv\Scripts\python.exe scripts/evaluate_retrieval.py --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml --split development --validate-only
.venv\Scripts\python.exe scripts/evaluate_retrieval.py --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml --split development --prepare-corpus --output tmp/stage1-knowledge-qa-v1-development.json
```
