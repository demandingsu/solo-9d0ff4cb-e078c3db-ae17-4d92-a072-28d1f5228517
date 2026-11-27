# 展厅播放器黑屏排查台（Flask + 极小极大推荐）

展厅播放器黑屏后，重启会清掉黑屏当下的面板代码、指示灯等现场读数。
本项目是一个数据驱动的 Flask 排查网页：载入 JSON 故障表（随附“展厅播放器
黑屏”样例，7 种故障、8 项检查），维修员录入实测读数、最多登记一次重启，
网页用极小极大决策 DP 推荐下一步，使**查明故障的最坏剩余耗时最短**。

## 功能与对应需求

| 需求 | 实现 |
| --- | --- |
| Flask 排查页 + 载入 JSON 故障表 + 黑屏样例 | 服务端从 `data/black_screen.json` 载入；页面可再上传自己的 JSON；`GET /api/table/sample` 下载样例 |
| 最多 8 故障 / 10 检查 | 载入时强校验数量上限、id 唯一、预期读数矩阵完整 |
| 各故障在每项检查的预期读数、检查耗时、阶段、重启耗时 | 页面矩阵逐格列出；检查带 `重启前/重启后` 徽标与耗时；顶部/侧栏显示重启耗时 |
| 录入实测读数 | 每行输入框 + 预期值 `datalist`，可自由输入；回车/失焦保存 |
| 最多登记一次重启 | `POST /api/restart` 第二次返回 `409 RESTART_LIMIT`，页面按钮随之消失 |
| 推荐下一步，最坏剩余耗时最短 | `app/planner.py` 在 `(候选故障集合, 已做检查掩码, 是否已重启)` 状态上做极小极大 DP |
| 后续检查随读数调整 | 每录入一条即重新计算；推荐卡还可展开“不同读数后的下一步”预案；测试按推荐策略对每种故障仿真 |
| 重启也计时 | DP 把 `restart_seconds` 计入最坏耗时；侧栏有已发生耗时、最坏总耗时和重启正计时 |
| 重启后不能补做重启前检查 | 前端禁用未录入的 pre 行；后端对“重启后首次写入 pre 检查”返回 `409 PRE_CHECK_MISSED` |
| 更正旧读数保留重启状态和其他读数 | 读数按检查 id 局部 `PUT`，案例状态在服务端；清空/更正均不影响其他键与重启标记 |
| 只剩一种故障报结果 | 绿色结果卡，给出故障编号、名称与处置建议 |
| 读数与所有故障不符提示核对 | 红色提示卡，指出哪些取值不在任何故障预期中，或说明是组合矛盾；对应输入框标红 |
| 无法保证查明时列出分不清的故障组 | 按“当前仍可达的未做检查”的预期读数签名做等价分类，输出大小 ≥2 的组（样例中立刻重启会得到 3 组） |
| Docker Compose 启动 | `Dockerfile` + `docker-compose.yml` + `/healthz` 健康检查 |

## 快速开始

### 方式一：Docker Compose（推荐现场使用）

```bash
docker compose up --build
# 打开 http://localhost:8000
# 若 8000 被占用： HOST_PORT=8080 docker compose up
```

Compose 以只读方式挂载 `./data`。替换 `data/black_screen.json` 后
`docker compose restart`（或 `up --build`）即可换表。

### 方式二：本地 Python 运行

```bash
python3 -m pip install -r requirements.txt
python3 wsgi.py                 # 或 gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 wsgi:application
```

运行测试：

```bash
python3 -m pip install pytest
python3 -m pytest               # 32 passed
python3 scripts/smoke.py        # 需先在 8000 端口启动服务；22 项 HTTP 断言
```

## JSON 故障表格式

```json
{
  "title": "展厅播放器黑屏故障排查表",
  "restart_seconds": 90,
  "faults": [
    {"id": "F1", "name": "系统存储松动", "hint": "重新插拔 SSD"}
  ],
  "checks": [
    {
      "id": "C5",
      "name": "重启前面板故障代码",
      "phase": "pre",
      "seconds": 30,
      "detail": "黑屏当下查看，重启后会被清掉",
      "options": ["E07", "E08", "无代码"],
      "expected": {"F1": "E07", "F2": "E08"}
    },
    {
      "id": "C6",
      "name": "重启后开机 Logo",
      "phase": "post",
      "seconds": 60,
      "expected": {"F1": "卡在Logo", "F2": "卡在Logo"}
    }
  ]
}
```

- `phase`：`pre`/`重启前` 或 `post`/`重启后`（也接受 `before`/`after` 等写法）。
- `expected` 必须覆盖每个故障；读数比较做 NFKC、去空白、英文大小写不敏感与
  纯数字等价（`12.0` 与 `12` 相同）。
- 兼容中文键名（`故障/检查/阶段/耗时/预期读数/重启耗时` 等）以及
  “故障为行、检查为列”的矩阵写法（`faults[].readings`）。
- 限制：故障 ≤ 8、检查 ≤ 10（校验报错会逐条中文列出）。

## 推荐算法说明

状态由“仍一致的候选故障 + 已完成检查掩码 + 是否已重启”唯一确定。每个状态的
合法动作：

1. 做一项当前阶段允许、且对候选集有区分度的未做检查，代价为其 `seconds`，
   随后按不同预期读数进入不同子状态，取各分支最坏值；
2. 未重启时可重启，代价 `restart_seconds`，之后未做的 pre 检查永久不可用、
   post 检查解锁；
3. 停止并接受当前歧义。

递推：`V(s)=min_a [cost(a)+max_o V(s_o)]`，单故障状态 `V=0`。状态空间至多
`2^8 × 2^10 × 2`，带记忆化后毫秒级完成。

为了在“无法保证查明”时仍给出合理的尽力推荐，停止动作的代价设为
`BIG·(|C|-1)`，其中 `BIG = 全部检查与重启耗时之和 + 1`，大于任何真实路径耗时。
因此：

- 只要存在保证唯一查明的策略，DP 选出的就是**最坏剩余耗时精确最小**的策略，
  此时 `guaranteed=true`、`worst_remaining_seconds` 即所求秒数；
- 不存在保证策略时，优化目标退化为“先最小化最坏情况下残留的分不清故障数，
  再优化耗时”，页面会明确警示并列出等价故障组，不谎称能查明。

“能否保证查明”与静态等价分类完全一致：未重启时所有未做检查仍可达
（pre 现在做、post 重启后做）；已重启时只剩未做的 post 检查。测试用 300 张
随机表校验了该不变量。

## HTTP API

页面使用有状态接口（签名 cookie 中的 `case_id` 关联服务端内存案例）：

- `GET  /api/state`：当前表、状态与完整分析
- `PUT  /api/readings/<check_id>`，body `{"value": "..."}`：录入/更正一条读数
- `DELETE /api/readings/<check_id>`：清空一条（`touched` 保留，仍可更正）
- `POST /api/restart`：登记唯一一次重启
- `POST /api/reset`：清空本案例（保留故障表）
- `POST /api/table/load`：载入自定义 JSON（JSON 对象或 multipart `file`）
- `GET  /api/table/sample`：下载当前服务端表文件
- `POST /api/analyze`（别名 `/api/recommend`）：无状态分析，
  body `{"table": {...可选...}, "readings": {"C2": "..."}, "restarted": false}`

## 已知限制 / 未完成项（如实说明）

- 本开发沙箱没有安装 Docker / Docker daemon，因此 **`docker compose up`
  未能在本机实际跑过**；已用 PyYAML 校验 compose 文件、按镜像同样的
  gunicorn 命令在宿主机完成 22 项真实 HTTP 冒烟。Dockerfile 为标准
  `python:3.11-slim + gunicorn` 单 worker 配置。
- 案例状态保存在**进程内存**（单 worker 多线程），重启容器会丢失排查进度；
  面向单展厅/单人使用足够，多副本部署需换共享存储或改用无状态 `/api/analyze`。
- 未做真实浏览器自动化/截图验证；已通过 `node --check` 与 HTTP 层测试，
  前端为原生 JS。
- 读数匹配为归一化精确匹配（含大小写/数字宽容），未实现模糊匹配与量纲换算。
