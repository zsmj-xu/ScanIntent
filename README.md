# ScanIntent

从来源 Langfuse ClickHouse 读取记录，一个 `(project_id, trace_id)` 还原成一个会话，
身份来自 observation 的 `metadata.user_api_key_alias`。会话快照保存在项目独立 ClickHouse，
网页主要展示对话，每条消息保留首次记录它的 `observation_id` 及字段位置。
暂不做意图识别。

## 启动

### Docker Compose 部署（推荐）

只需要 Docker 和 Docker Compose，不需要在宿主机安装 Python。
在项目目录执行；新环境先创建配置：

```sh
cp .env.example .env
chmod 600 .env
```

完整配置和每项说明见 [`.env.example`](.env.example)。必须替换 5 项：
`SCANINTENT_PROJECT_ID`、`SCANINTENT_CLICKHOUSE_PASSWORD`、`SOURCE_CLICKHOUSE_HOST`、
`SOURCE_CLICKHOUSE_USER` 和 `SOURCE_CLICKHOUSE_PASSWORD`。
其余项可保留默认值，但来源库的端口、数据库名和 HTTPS 开关须与实际环境一致。
可用 `openssl rand -hex 24` 生成目标库密码。
已有 `.env` 时直接补充 `SCANINTENT_PROJECT_ID`，保留原目标库密码，不要用示例覆盖。
来源库使用容器可访问的地址；`127.0.0.1` 在容器中表示容器自身。

```sh
docker compose up -d --build --wait
```

启动 ClickHouse 和网页服务，数据库健康后自动初始化表结构。
打开 http://127.0.0.1:8765；已有项目数据仍使用原来的独立 Docker volume。
端口被占用时在 `.env` 设置 `SCANINTENT_HTTP_PORT=18765`，重新执行
`docker compose up -d` 后访问该端口；单纯 `restart` 不会更新端口映射或容器环境变量。
网页和数据库端口默认仅绑定宿主机 `127.0.0.1`。

检查来源连接、手动采集一轮：

```sh
docker compose exec app python scanintent.py doctor-source
docker compose exec app python scanintent.py collect --project-id '项目ID' \
  --start '2026-09-14T14:30:00+08:00' --max-traces 100
```

采集完成后刷新网页；部署不会自动采集或启动定时任务。
容器采集统一通过 `docker compose exec app` 执行，使写入进程共用同一把文件锁；
不要同时运行宿主机采集命令或另起容器写入同一目标库。

```sh
docker compose ps
docker compose logs --tail 100 app
docker compose down
```

`down` 保留数据库数据。更新代码后重新执行 `docker compose up -d --build --wait`。
`.env` 只读挂载到应用容器，不进入镜像；真实对话和验证产物也不进入构建上下文。

### 本机 Python 启动

Python 3.9+，仅标准库；数据库仍通过 Docker Compose 启动。

```sh
python3 scanintent.py init-local
docker compose up -d --wait clickhouse
python3 scanintent.py init-db
python3 scanintent.py doctor-source
```

`init-local` 生成随机目标库密码并保存到权限为 `0600` 的 `.env`，不覆盖已有配置。
不要打印或提交该文件。来源连接从本项目 `.env` 的 `SOURCE_CLICKHOUSE_*` 读取，
目标连接只使用 `SCANINTENT_CLICKHOUSE_*`，互不混用。
目标 CK 固定镜像摘要，只绑定 `127.0.0.1:18124`，数据在独立 Docker volume 中。
不要使用 `docker compose down -v`，它会删除项目存储。

新环境在 `init-local` 后，将来源库配置添加到同一个 `.env`：

```dotenv
SOURCE_CLICKHOUSE_HOST=your-source-host
SOURCE_CLICKHOUSE_PORT=8123
SOURCE_CLICKHOUSE_DATABASE=langfuse
SOURCE_CLICKHOUSE_USER=your-readonly-user
SOURCE_CLICKHOUSE_PASSWORD=your-source-password
SOURCE_CLICKHOUSE_SECURE=0
```

来源库使用只读账号；HTTPS 连接按实际端口配置，并设置 `SOURCE_CLICKHOUSE_SECURE=1`。

## 只采集一轮

```sh
python3 scanintent.py collect --project-id '项目ID' \
  --start '2026-09-14T14:30:00+08:00' --max-traces 100
python3 scanintent.py serve --project-id '项目ID'
```

打开 http://127.0.0.1:8765 查看列表、对话、消息来源和采集耗时。
网页只读，不触发采集，也不定时刷新。采集命令处理一个完整 30 分钟窗口后退出，**没有后台循环或定时任务**。

网页交互：

- 会话列表支持标题 / trace ID 搜索、身份选择、最后活跃日期范围、还原提示筛选和新旧排序；筛选作用于全部已保存会话。
- 列表按 20 / 50 / 100 条分页，显示匹配总数，支持指定页跳转；日期按浏览器本地时区，结束日期包含当天。
- 点击会话在抽屉中预览，可切换完整阅读页；关闭抽屉后保留原列表位置，支持 Esc、键盘焦点和背景滚动锁定。
- 筛选、页码、会话、阅读模式和消息视图保存在 URL，刷新或浏览器前进 / 后退可恢复；`/` 聚焦列表搜索。
- 对话与全部记录分开查看，每页最多显示 40 条，超长正文可展开，消息来源默认折叠；详情接口只返回当前页消息，切换视图或翻页时再读取，最近三页缓存在浏览器内存中。数据库仍读取完整会话快照。
- 采集记录为独立子页，进入时才请求数据，阶段耗时按需展开。加载失败可重试，刷新时只更新当前页面。

未指定 `--start` 时使用来源服务器当前时刻之前 30 分钟；时间参数必须有时区。
`--resume` 手动读取上次成功窗口的下一个 30 分钟窗口，仍然只执行一轮。
`--alias` 可限定身份；`--trace-id` 可重复指定，供小范围验证。
首次测试建议显式使用 `--max-traces 100`，达到上限但窗口还有记录时标记 `partial`，不推进进度。
省略该选项才会处理整个窗口；本项目真实历史窗口中曾发现 13,741 条 trace，不宜直接当作小样本测试。
不同项目、身份和 trace 筛选条件分别维护进度。

先在 `[start, start + 30min)` 的 **observations.event_ts** 范围内发现 trace，
然后用轻量字段定位这些 trace 的历史 observation IDs、类型和日期，
按完整排序键选择性读取全部有效 observations 正文，不再按发现窗口截断历史。
默认每批 20 条 trace，可用 `--batch-size 1..100` 调整。
响应按行解析，正文按批读取、排序，每次还原一条 trace，再批量写入快照；不会把整个窗口的全部原始正文一次读进内存。
一批 trace 的完整历史及还原结果仍需要内存；遇到超长会话应降低 `--batch-size`。

## 来源时间

实查来源服务器为 `Asia/Shanghai`，DateTime64 字段没有显式时区。
已知样本中，同一 trace 的 `traces.timestamp` 显示约 23:48，
observation 的 `start_time` 显示约 15:48，存在约 8 小时差异。
这不是已确认可统一修正的偏移，因此不自动加减 8 小时，也不使用 trace timestamp 发现会话。

程序通过 Unix 毫秒时间读取和比较字段，用带时区的输入确定采集窗口；
保留原有 observation 时间，网页按浏览器本地时区显示。
每轮保存来源时区、服务器时钟、本机时钟偏差、往返耗时和所用时间字段。
`event_ts` 是当前选择的来源事件版本时间，**尚不能保证它等于可靠的入库时刻**。
若上游回填旧 event_ts，已经走过的窗口需要手动重扫；成功空窗口也只表示该查询范围无匹配记录。

来源按 `start_time` 分区和排序，`event_ts` 没有对应索引。
为了不漏掉延迟到达的旧会话，目前发现查询需要扫描项目范围；这会影响大库性能。
没有擅自用最近若干天的 start_time 限制来换取速度，后续应按真实扫描耗时评估来源索引或可靠入库时间。

## 还原与存储

- 按 `start_time, observation_id` 顺序处理快照，按序列重叠合并重复历史。
- 新消息归属首次记录它的 observation，后续重复携带不改变归属；真实的重复发言按位置保留。
- 工具调用使用调用 ID 对齐，兼容 OpenAI tool_calls 与 Anthropic tool_use/tool_result。
- 已识别的用户确认工具，其返回值显示在问题与后续执行之间；普通工具结果默认折叠。
- 参数校验失败的确认请求归为工具过程；真实同意或拒绝保留。来源 `level=ERROR` 归为调用失败。
- 系统注入、技能上下文、压缩摘要默认折叠；已知标题、建议输入、离开后摘要、安全检查辅助调用不混入主对话。
- 标题 JSON 支持代码围栏和 session 封装；结合后续主历史同一需求佐证，保留真正的用户标题请求。
- 运行时压缩模板与空输出重试归为内部事件；同一块中位于压缩模板前的真人文本仍保留。
- 明确的 Agent/Task prompt、WebSearch query 与父工具调用关联，子任务单独还原并折叠，不能当真人发言。
- 客户端 AGENTS/environment/task-notification/bash 封装归为上下文；system 中固定封装的真人中途插话需解包保留。
- 同一原始消息的多个可见文本/附件块合并为一条对话消息，逐块保留 observation/字段/字符位置；列表与详情使用相同计数。
- 上传资源清单转为附件描述，不回显下载 URL/pod 路径；读图工具的图片说明归为工具上下文，保留图片占位。
- 保留会话原文，不做自动脱敏；还原规则只区分真人消息、模型回复与内部过程。
- 压缩前的可用历史继续保留。缺失压缩前原文、无法对齐或出现分支时标记，不推测不存在的消息。
- 一条会话一份完整快照，包含摘要字段、消息 JSON 和 observation IDs；只查询列表字段时不读取正文列。
- `ReplacingMergeTree(version)` 按 `(project_id, trace_id)` 替换，所有用户读取使用 `FINAL`，不依赖后台合并完成。
- 批内成功还原后写快照，不先删除旧数据；采集成功日志本身是进度。失败窗口不推进，可重新执行。
- 本机文件锁保证一次一个写入进程；多机采集与可靠分布式锁不在第一版范围内。

跨窗口重读同一 trace 时会重新构建完整快照。单个会话很长且频繁更新时会产生写入放大。
完整性取决于来源保留的历史；仅保存 observation IDs，不能保证来源删除后仍能重新查询原始记录。

## 阶段耗时

每轮 `collection_runs` 持久保存状态、窗口、会话数、observation 数和以下累计毫秒耗时，网页可查看：

| 字段 | 阶段 |
| --- | --- |
| `clock_ms` | 来源时钟查询 |
| `discovery_ms` | 发现 trace 的网络请求及响应读取 |
| `lookup_ms` | 轻量扫描，定位完整历史的 IDs、类型及日期 |
| `history_ms` | 完整 observations 的网络请求及响应读取 |
| `parse_ms` | 来源响应 JSON 行解析 |
| `ordering_ms` | 将分块读取的 observations 排回时间顺序 |
| `reconstruct_ms` | 正文解析、规范化、消息序列还原 |
| `persist_ms` | 会话快照批量写入 |
| `checkpoint_ms` | 续采时读取上次成功进度 |
| `run_log_ms` | 创建本轮执行记录 |
| `total_ms` | 来源时钟检查开始，到发布最终执行记录之前 |

网络阶段包含服务器执行及传输，不代表数据库内部耗时。
阶段计时不包含消费生成器时执行的其他处理；未执行阶段为 0。
目标表初始化、配置文件读取和最终执行日志自身的写入不计入这张表；`run_log_ms` 还包括批间进度日志。
失败也保存已完成阶段及安全错误类别；进程被强杀时可留下 `running` 记录，需要重跑原窗口。

## 验证

```sh
python3 -m unittest -v test_ck_reader test_scanintent
SCANINTENT_TEST_CK=1 python3 -m unittest -v test_scanintent.ClickHouseIntegrationTests
```

第二条会读写本项目目标 CK 中随机生成的测试项目，结束后清除这些测试数据。
真实对话样本不随代码分发；若本机存在原有两份样本，则同时校验原先确认的清洁对话没有丢失。

### 测试阶段：代码整理，再独立审核

不能只验证一个 trace。按压缩、确认/拒绝、失败重试、子任务、附件、混合工具/真人文本、
Codex 压缩快照等结构挑选多条真实案例，并保留普通对话的负例对照。

```sh
python3 validate_trace.py --project-id '项目ID' --trace-id '指定traceID' \
  --raw-file '.verification/指定traceID/raw.json' \
  --output-dir '.verification/指定traceID/round-1'
```

该命令离线读取已保存的原始 observation 数组，生成 `candidate.json`、`dialogue.json` 和 `manifest.json`；
不会写入 CK。输出目录必须全新，权限 `0700`，JSON 权限 `0600`，整个 `.verification` 不提交。
manifest 固定原始文件、候选和规则代码的 SHA256，防止审核过程中证据被替换。

随后由 subagent 从原始记录独立推导并逐条审核，不能用代码自身重放、消息条数减少或单测通过代替正确性结论。
检查原文内容/角色/顺序/重复/遗漏、首次来源、确认位置、压缩连续性、子任务边界与附件。
发现问题先修通用规则、补负例测试，在新轮目录重新生成并复审。只将审核通过且哈希一致的候选写回指定 trace。
原始导出缺少错误级别/身份信息、压缩前历史或助手正文时，要在报告与页面明确局限，不能猜测补齐。

### 跨真实 trace 的规则验收（2026-10-15）

按“代码整理 → subagent 独立审核 → 修规则与负例 → 新证据复审”执行。
3 个独立审核 agent 共核对 8 条真实 trace、1,449 条 observations，最终保留 674 条主对话消息；
按用户要求保留原文，不做脱敏。所有最终候选的原始数据、代码、候选和审核报告哈希一致。

| traceID | observations | 主对话消息 | 覆盖范围 |
| --- | ---: | ---: | --- |
| `02d3df61-d6f8-426e-a154-3102c00059a4` | 152 | 55 | 压缩、确认失败与回答、附件、调用错误 |
| `1a9e7a31-dd90-405c-9590-f2fbb6adb54d` | 153 | 88 | 标题围栏、审批回放、system 中的真人插话 |
| `81ae2eb9-2119-4483-82b2-0b01e7301e5a` | 192 | 31 | 搜索子分支、压缩空重试、插话、失败与中断 |
| `cf9e957a-b45d-4b61-9070-be0e45dcd305` | 898 | 440 | 282 个辅助调用、65 条子分支记录、17 次有效确认、长历史 |
| `0201d239-61f4-4143-b704-a0c9bfba45d1` | 1 | 28 | 单记录长历史、混合工具结果与真人输入 |
| `02384b90-c685-4ac7-9a51-8383c980ade6` | 1 | 10 | Codex 封装、handoff 摘要、缺失助手历史 |
| `0e1c4241-c517-4e72-994a-f41fd9b2bb73` | 22 | 6 | 原有普通对话与工具对照 |
| `2bd12f1d-ce4e-4443-80d2-eed16cf001bf` | 30 | 16 | 原有授权对话、Read 图片工具附带说明 |

最终证据位于 `.verification/<traceID>/conversation-only/`，汇总为 `.verification/cross-trace-summary.json`。
6 条具备当前来源字段的在线案例已通过回读验证并保存到目标 CK；两条旧导出只作内容对照，
其缺少错误级别及身份 metadata，未作为新的在线会话写入。
`02384…` 的 10 条真人请求还原通过，但来源缺少助手正文，明确标记为不完整，未伪造回复。
44 项检查（含独立 CK 集成）通过。当前不启动持续采集。

### 首轮实测（2026-10-15）

使用上海时间 `2026-09-14 14:30–15:00` 的历史窗口，来源发现 13,741 条 trace。
按“先采样检查”的要求主动中断，保留 180 条会话、333 条 observation、460 条消息；
执行状态为 `interrupted`，没有推进整窗进度，也没有启动持续采集。
独立目标 CK 重启后上述数量不变；26 项本地及 CK 集成测试全部通过，
浏览器验证了身份筛选、对话展示、observation 来源展开。

最初直接按 traceID 读正文发生超时；改为定位完整排序键后可完成读取。
这次样本执行约 251 秒，其中发现 trace 约 26.9 秒、正文请求约 219.2 秒、
定位历史 IDs 约 2.6 秒、还原约 0.8 秒、写入约 0.4 秒。
随后进一步将短会话的正文请求合并为批量请求；最终代码的该项优化通过了针对性测试，
但尚未再次实采测量速度。真实用户确认和上下文压缩案例，以及整窗吞吐量仍需后续补验。

## 原有验证读取接口

只用 Python 标准库，通过 HTTP 只读连接来源 Langfuse ClickHouse。
默认从本项目 `.env` 读取 `SOURCE_CLICKHOUSE_*`，兼容旧 `CLICKHOUSE_*` 名称。
环境变量覆盖文件中的同名配置；运行时不依赖相邻项目的配置文件。
不复制或打印凭据。

```python
from ck_reader import read_observations

timings = {}
groups = read_observations(
    project_id="你的项目ID",
    start="2026-10-14T16:00:00+08:00",
    end="2026-10-14T16:10:00+08:00",
    timings=timings,
)
print(timings)  # 各阶段耗时，单位毫秒
```

也可以直接运行：

```sh
python3 ck_reader.py --project-id '你的项目ID' \
  --start '2026-10-14T16:00:00+08:00' \
  --end '2026-10-14T16:10:00+08:00'
```

结果是 `{trace_id: [记录, ...]}`，每条记录只含 `trace_id`、`name`、`metadata`、`input`、`output`。
`tracesID` 对应数据库的 `trace_id`，`matedata` 对应 `metadata`。
`metadata` 是字典，`input/output` 保留数据库原始字符串或 null。

项目和起止时间必填，时间必须带时区，按 observations 的 `start_time` 筛选，起点包含、终点不包含。
一次查询读取范围内全部 observations，再按 trace ID 分组，
按时间排序；范围外的历史不读取。查询使用 `FINAL` 去除同一 observation 的旧版本，并排除已删除记录。
不额外限制 observation 类型，也不要求 session ID。

不设置 trace 数量、记录数、扫描行数、响应大小或查询执行时间上限，HTTP 客户端也不设置超时。
服务器或网关自身的限制仍可能生效。全部结果在内存中保存，内存占用随数据量增加。
输出包含原始对话，真实结果不要提交到仓库。

换配置文件使用 `--env-file /path/to/.env`；只使用环境变量时，函数传 `env_file=None`。

传入 `timings={}` 获取本次调用的耗时，原分组返回结构不变；每次调用会重置这个字典。
命令行默认把耗时打印到 stderr，stdout 仍然只输出结果 JSON；失败时也会输出已执行阶段的耗时。

| 字段 | 含义 |
| --- | --- |
| `config_ms` | 读取配置、准备连接参数 |
| `observations_query_ms` | 读取 observations 的 HTTP 请求及响应读取 |
| `field_processing_ms` | 响应的 JSON 行解析 |
| `grouping_ms` | 按 trace ID 分组 |
| `total_ms` | 整个读取方法耗时，包含参数校验、SQL 准备等开销 |

查询耗时包含网络传输与服务器执行，不单独代表数据库内部耗时。
未执行的阶段为 0；计时采用单调高精度时钟，结果不包含调用后的 JSON 输出耗时。

运行本地检查：`python3 -m unittest -v test_ck_reader`。
