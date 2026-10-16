# ScanIntent

从来源 Langfuse ClickHouse 只读采集，按 `(project_id, trace_id)` 还原会话，
写入独立目标 ClickHouse，提供只读网页。没有自动采集或意图识别。

## 运行与验证

- 配置说明以 `.env.example` 为准，操作说明见 `README.md`。
- 完整启动：`docker compose up -d --build --wait`。
- 本地检查：`python3 -m unittest -v test_ck_reader test_scanintent`。
- 数据库集成：`SCANINTENT_TEST_CK=1 python3 -m unittest -v test_scanintent.ClickHouseIntegrationTests`。
- Python 3.9+ 标准库，Docker 应用镜像使用固定摘要的 Python 3.12。

## 文件与边界

- `scanintent.py` 是入口；`collector.py` / `source.py` 负责采集，
  `reconstruction.py` 负责还原，`store.py` / `clickhouse.py` 负责存储。
- `web.py` 和 `static/` 提供网页，`schema.sql` 定义目标表。
- `ck_reader.py` 同时提供共享来源连接与独立只读查询，不是废弃脚本。
- `validate_trace.py` 生成离线审核证据；还原规则修改按 README 的独立审核流程验证。
- `.env` 仅保存在项目内，不读取相邻项目配置，不提交凭据或真实对话。
- 私有回归样本放 `.verification/samples/`，其他审核证据保留在 `.verification/`。
- 不删除仍被进程使用的 `.collection.lock`，不删除数据库卷。
- 采集通过同一个 app 容器执行，避免跨容器或宿主机并行写入。
- 发布推送和容器测试均不等于实际服务已经更新，交付时明确验证范围。
