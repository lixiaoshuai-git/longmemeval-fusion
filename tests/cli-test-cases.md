# CLI 测试用例（手工执行）

覆盖 `cli.py` 全部命令 + 异常边界。A/B/D 组零 LLM 成本（走评测缓存），
C 组会产生少量真实调用（已标注）。执行完在末尾记录表打勾。

## 准备

```powershell
cd C:\Users\Administrator\WorkBuddy\2026-09-23-10-57-19\dive
# 复制一份缓存，避免污染评测缓存原件（provenance）
copy results\fusfull345-fusion-cache.json results\cli-test-cache.json
# 启动（注意：必须用托管 venv 的 python，系统 python 没装 openai）
& "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" cli.py --cache results\cli-test-cache.json
```

启动成功标志：出现横幅 `D.I.V.E. 记忆系统 CLI —— /help 查看命令，/quit 退出`，
提示符 `mem>`。

> 不带 `--cache` 直接启动也可以，但 TC-03 的 `/load` 会触发真实抽取
> （约 25 次 LLM 调用/会话），慢且花钱。下文所有 `python` 均指上面的
> venv 全路径，简写为 `%PY%`；PowerShell 里可先执行
> `$PY = "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"`。

---

## A 组 · 冒烟（零 LLM 成本）

### TC-01 启动与帮助
| | |
|---|---|
| 操作 | 启动后输入 `/help` |
| 预期 | 列出 6 条命令：load / ingest / ask / context / stats / forget，各带一行说明 |
| 验证点 | 无 ask 的"裸输入"说明行（裸输入行为在 TC-08 验证） |

### TC-02 未知命令
| | |
|---|---|
| 操作 | `/foo` |
| 预期 | `未知命令 /foo（/help 查看命令）`，**会话不退出**，提示符继续 |
| 验证点 | REPL 健壮性 |

### TC-03 载入题目（缓存全命中）
| | |
|---|---|
| 操作 | `/load 3a704032` |
| 预期 | 依次打印 `加载数据集…`，3 个会话：`会话 1/3 @ 2023/05/21 (Sun) 13:14 完成 \| 事件 23` → `2/3 … 事件 40` → `3/3 @ 2023/05/23 … 事件 60`，最后 `当前日期设为问题日期: 2023/05/28 (Sun) 09:22` |
| 验证点 | **秒级完成、无网络等待**（= 零 LLM 调用）；事件数 23→40→60 递增 |

### TC-04 载入不存在的题
| | |
|---|---|
| 操作 | `/load deadbeef` |
| 预期 | `没有这道题: deadbeef`，会话继续 |

### TC-05 规模报告
| | |
|---|---|
| 操作 | `/stats` |
| 预期 | `{'events': 60, 'evidence': 36, 'extracted': 0, 'merged': 5, 'superseded': 2, 'invalidated': 1, 'added': 60}` |
| 验证点 | events=总数（含 superseded/invalidated），evidence=36 轮 |

### TC-06 确定性检索上下文（不调模型）
| | |
|---|---|
| 操作 | `/context How many plants have I bought in total?` |
| 预期 | 打印约 **1.2 万字符**的检索上下文，含 `[FACT MEMORY]` 段、`Query: agg_count` 行、事件列表（含 TOTAL MATCHED 行） |
| 验证点 | 这是回答该题所依赖的全部记忆证据（B.5 可追溯性） |

---

## B 组 · 提问（每次 /ask 约 1 次 LLM 调用 ≈ 3-4k token）

### TC-07 基于记忆回答
| | |
|---|---|
| 操作 | `/ask How many plants have I bought in total?` |
| 预期 | 先输出答案（一个具体数字，该题历史上 3/4 均有可能），随后一行 `[trace] {'intent': 'agg_count', ...}` |
| 验证点 | trace 与 TC-06 的 Query 行一致 |

### TC-08 裸输入视为提问
| | |
|---|---|
| 操作 | 不带斜杠直接输入 `What pets do I have?` |
| 预期 | 与 `/ask` 相同：给出答案 + trace（记忆里没有宠物记录时应倾向"未提及"而非编造） |

---

## C 组 · 写入与遗忘（/ingest 少量抽取调用；/forget 全免费）

### TC-09 写入自定义会话
| | |
|---|---|
| 操作 | `/ingest tests\fixtures\sample-session.txt 2025-11-01` |
| 预期 | `已写入 3 轮 @ 2025-11-01`，stats 中 `evidence` 由 36 → **39**，`events` ≥ 61 |
| 验证点 | U:/A: 前缀解析正确（3 轮而非 3 行合并成 1 轮） |

### TC-10 forget 缺参数
| | |
|---|---|
| 操作 | `/forget` |
| 预期 | `至少给一个策略参数，例如: /forget --capacity 30 --compact 180` |

### TC-11 容量策略（软归档）
| | |
|---|---|
| 操作 | `/forget --capacity 40` |
| 预期 | `归档 18 条: {'capacity': 18}`，列出前 8 条 `[capacity] 日期 \| 事实`，末行 `归档后: {'events': 61, 'active': 40, ...}` |
| 验证点 | **events 总数不变**（61）——软归档不删数据；active 精确降到 40 |

### TC-12 遗忘后检索仍正常
| | |
|---|---|
| 操作 | 再跑一次 TC-06 的 `/context How many plants have I bought in total?` |
| 预期 | 正常返回、**字符数明显少于 TC-06**（部分植物事件已归档退出召回），无崩溃 |
| 验证点 | 遗忘不影响检索路径健壮性 |

### TC-13 幂等性
| | |
|---|---|
| 操作 | 再跑一次 `/forget --capacity 40` |
| 预期 | `归档 0 条: {'capacity': 0}` |
| 验证点 | 重复执行不重复处理 |

---

## D 组 · 异常与边界（零 LLM 成本）

### TC-14 不存在的文件
| | |
|---|---|
| 操作 | `/ingest nofile.txt 2025-01-01` |
| 预期 | `[error] FileNotFoundError: …`，**会话不退出** |

### TC-15 非法日期
| | |
|---|---|
| 操作 | `/ingest tests\fixtures\sample-session.txt 2025-13-99` |
| 预期 | `用法: /ingest <file> <YYYY-MM-DD>  (…)`（ValueError 被捕获转提示） |

### TC-16 非法 forget 参数
| | |
|---|---|
| 操作 | `/forget --capacity abc` |
| 预期 | 用法提示，不崩溃 |

### TC-17 退出
| | |
|---|---|
| 操作 | `/quit` |
| 预期 | 立即退出、无报错。重启后按 `Ctrl+C`（或 `Ctrl+Z` 回车）→ 同样优雅退出 |

---

## 进阶验证（可选，退出后执行）

验证"归档事件退出当前召回、但历史时刻仍可审计"：

```powershell
& $PY tests\audit-after-forget.py
```

预期：`archived=34`，`value_at(...) -> 命中(可审计)`。
（若你在 TC-11 已对同一缓存做过 capacity 归档，archived 数会 >34——正常，
说明归档状态在缓存外的内存里累计；换新副本重跑则精确 34。）

## 记录表

| 用例 | 结果（✓/✗） | 备注 |
|---|---|---|
| TC-01 | | |
| TC-02 | | |
| TC-03 | | |
| TC-04 | | |
| TC-05 | | |
| TC-06 | | |
| TC-07 | | |
| TC-08 | | |
| TC-09 | | |
| TC-10 | | |
| TC-11 | | |
| TC-12 | | |
| TC-13 | | |
| TC-14 | | |
| TC-15 | | |
| TC-16 | | |
| TC-17 | | |
