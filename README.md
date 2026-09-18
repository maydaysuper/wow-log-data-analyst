# WoW Log Data Analyst v0.18.0 — 工程加固与自审版

原生 PySide6 WoW Retail 战斗分析工具。v0.18 不继续堆新的指标，而是重点解决长期使用时最影响体验的工程问题：WCL 重复请求和 Token 刷新、AI 报告重复生成、Windows 真机打包验证，以及把自动测试、Windows 真构建和自审做成发布门禁。

## v0.18 核心变化

- **WCL 单飞请求（single-flight）**：多个后台线程同时请求同一 Report / Events 时，只允许一个线程真正访问 WCL，其余线程等待并复用结果，减少重复 points 消耗。
- **共享 OAuth 状态**：WCL worker `fork()` 继续使用各自 HTTP Session，但共享 Token、刷新锁和缓存；并发场景不会每个 worker 各自刷新 Token。
- **并发 401 恢复**：Token 失效时自动重新认证一次；多个 worker 同时拿到 401 时只刷新一次，新 Token 不会被后来到达的旧 401 再次清掉。
- **WCL 诊断指标**：内部记录网络查询数、Token 刷新次数、缓存命中/未命中、single-flight 等待、认证重试和当前缓存/在途请求数，方便继续定位 API 消耗。
- **DeepSeek 报告缓存**：同一份结构化证据、个人模型、同专精知识、模型和分析模式完全一致时，15 分钟内直接复用已经生成的教练报告，不再重复两次/三次调用 AI。
- **冻结 EXE 自检**：Windows PyInstaller 构建后会真正执行 `WoW Log Data Analyst.exe --self-test`，自检失败则构建失败，不再只判断文件是否生成。
- **GitHub Actions Windows 构建**：PR 和 main 都会跑 Ubuntu/Windows 自动测试；Windows runner 还会构建真正的 Portable EXE ZIP 并上传 Artifact。
- **版本资源随包发布**：桌面程序从 `VERSION` 读取版本，Mac/Windows frozen package 都会带入 VERSION，减少程序、脚本、文档版本漂移。
- **锁顺序审查**：WCL 缓存、single-flight、统计锁不再嵌套反向获取，避免高并发下潜在死锁。

## 现有分析能力

- WCL 角色 → 最近限时成功大秘境 → 多选 → 一键 AI 分析。
- 个人模型：同专精/同版本/同副本/相近层数历史基线，增量同步与长期总结。
- 同职业同专精横向样本：排名只用于发现候选，结论基于重新读取的真实 Report/Fight 事件。
- Cast / Damage / Buff / Resource / Death / Interrupt / Pull 时间轴；战斗内 Buff 覆盖、技能节奏、爆发重合。
- 异常停手只在 **战斗中 + 玩家存活 + 队友持续作战** 时成立，死亡/复活/跑图/全队停手排除。
- BOSS / 常见优先集火目标 / 重要大怪学习，以及同专精关键目标伤害横向图表。
- AI 报告以简体中文教练语言输出；后台统计仍可使用分位数，但正式报告默认翻译为玩家能理解的表达。

## GitHub 自审流程

仓库使用 `AGENTS.md` 和 `docs/AI_REVIEW_WORKFLOW.md` 记录工程约束：

1. 在独立 Issue/分支/PR 实现；
2. 提交前对真实 diff 做 BLOCKER / IMPORTANT / OPTIONAL 自审；
3. BLOCKER/IMPORTANT 必须修复或用测试证据证明不成立；
4. GitHub Actions 运行 Ubuntu/Windows 测试并在真实 Windows runner 构建 Portable EXE；
5. 自动检查通过后仍由用户决定是否合并与发布。

外部 reviewer bot 不是 v0.18 发布的强制条件。

## 测试

当前本地自动测试：**86 / 86**，并通过 `python -m compileall -q desktop_app.py core tests`。

## Mac 测试构建

解压后双击 `Build Mac Test Version.command`。脚本会先运行测试，再在 macOS 本机生成 DMG。

## Windows 测试构建

在 Windows 上双击 `Build Windows Test Version.cmd`，会生成 onedir 便携版 `WoW Log Data Analyst.exe` 和 `WoW-Log-Data-Analyst-v0.18.0-Windows-Portable.zip`。onedir 是有意选择：相比 onefile，PySide6/Pandas 程序不需要每次启动先解压大体积运行时。

GitHub PR 的 `Windows portable EXE` job 也会在真实 `windows-latest` runner 构建并执行 frozen self-test，成功后上传 Portable ZIP Artifact。
