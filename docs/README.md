# docs — Winnow 文档库

所有项目文档归这里。仓库根目录只留 `README.md`（门面）、`README.zh-CN.md`（中文快照）、`LICENSE`。

| 文件 | 内容 |
| --- | --- |
| `PLAN.md` | **设计/实施唯一真源**：D1–D12 已拍板决策、各阶段详设、验收口径 |
| `CONTRIBUTING.md` | 工程约定：PEP 723 stage 形态、just 驱动、测试矩阵 |
| `ops.md` | systemd user units + just 配方公共前奏（prelude.sh/\_jlock） |
| `composer.md` | Remotion 合成器用法（手工/冒烟路径）+ 可行性实验记录 |
| `vendored-upstream.md` | juya-news-card 定格 SHA、本地 patch 清单、重新同步上游方法 |

根目录留着不进 docs/ 的"文档"都是**运行时输入**而不是文档：`rulebook.md`（filter/digest 直接读）、`sources.yaml`、`aliases.json`、`sensitive_words.txt`、`config.example.yaml`、`secrets.env.example`。

调研档案区（`experiments/`、`repro/`、`evidence/`）不随仓发布——PLAN 里各处"种子：`experiments/…`"是出处标注，本地保留可查。
