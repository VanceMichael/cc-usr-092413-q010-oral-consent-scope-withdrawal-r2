# 口述资料同意范围与撤回处置

服务以 Django REST Framework（此处用 Django 视图）和本地 SQLite 提供口述资料授权后端：
同意八字段版本化、材料派生谱系、最小可见清单、职责分离、撤回/收窄处置、
领取并发一致性、可解释查询，以及可中断续跑的到期/下架作业。

## 领域规则

- **同意（Consent）** 每次固定八个维度：讲述人 `narrator_code`、采集场次
  `session_code`、用途 `purpose`、受众 `audience`、地区 `region`、期限
  `valid_from/valid_until`、署名方式 `attribution`、证据摘要 `evidence_summary`。
  同一 (讲述人, 场次, 用途) 形成版本链，任何变化产生新版本并保留 `supersedes`。
- **材料（Material）** 支持录音/主题片段/转写/翻译/展陈节选。同一标识再次登记为
  新内容版本（`code + content_version` 唯一）；派生材料保存指向具体父本版本的
  `derived_from` 与来源范围快照 `source_consent`。
- **最小可见清单**：研究/展陈申请只返回与用途、受众、地区、日期相符的项；其余项
  给出遮蔽（`audience/region_not_covered` 等）或必须重新申请
  （`revoked_reapply`、`expired_reapply`）的原因。
- **幂等与人工核查**：相同 `request_key` 且申请内容相同，重试沿用原决定；
  同键异内容、或同标识材料指纹变化，转 `manual_review`。
- **职责分离**：档案员整理范围并提交、伦理复核人确认限制、发布人执行开放；
  任何人不能批准自己提交的同意或节选（提交人、确认人、发布人三者互异）。
- **撤回/收窄**：停止尚未完成的访问（`Grant.state=stopped`）并阻断后续发布；
  已完成阅览保留为审计事实；公开材料不删除历史，进入下架 → 通知 → 回执流程。
- **领取并发**：领取时对相关同意链加锁并重算清单版本，清单与文件凭据必须基于
  同一同意版本；版本已变则整批拒绝、要求重新申请，绝不发放部分凭据。
- **作业续跑**：到期与下架作业以游标检查点（down/notify/receipt 各步幂等），
  中断后继续原任务，通知、回执靠唯一约束去重。
- **可解释查询** `GET /explain?purpose=…&code=…&date_from=…&date_to=…`
  返回每项为何可见/遮蔽/重新申请、所依据同意版本、来源谱系与派生材料处置状态。

## 接口（均以 `X-Actor-Code` 头标识操作人）

| 方法 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /materials` | 档案员 | 登记/派生材料（`parent_code` 可带 `parent_version`） |
| `POST /consents` | 档案员 | 提交同意并固定范围 |
| `POST /consents/{id}/confirm` | 伦理人 | 确认限制（可带 `restrictions`） |
| `POST /consents/{id}/open` | 发布人 | 执行开放（收窄版本开放时同步生效） |
| `POST /consents/narrow` | 档案员 | 提交范围收窄新版本 |
| `POST /consents/{id}/revoke` | 家属 | 撤回该讲述人/场次全部用途 |
| `POST /requests` | 申请人 | 申请，返回最小可见清单与决定 |
| `GET  /requests/{key}` | 申请人 | 查看原决定（幂等） |
| `POST /requests/{key}/claim` | 申请人 | 领取文件凭据（同意版本一致性裁决） |
| `POST /grants/{id}/complete` | 申请人 | 阅览完成，固化审计事实 |
| `GET  /explain` | 授权人员 | 片段/日期/用途可解释查询 |
| `POST /jobs/{id}/run` | 运维 | 续跑指定到期/下架作业 |
| `GET  /jobs/{id}` | 运维 | 查看作业游标与状态 |

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 生成/应用迁移：`python3 manage.py makemigrations consent && python3 manage.py migrate`
- 运行测试：`python3 -m unittest discover -s tests -v`（亦支持 `python3 manage.py test`）
- 编译或构建检查：`python3 -m compileall -q project consent manage.py`
- 处理到期/下架作业：`python3 manage.py process_jobs [--no-sweep] [--job-id ID] [--today YYYY-MM-DD]`
- 启动服务：`python3 manage.py runserver 0.0.0.0:8080`

测试使用仓库内临时 SQLite，不连接外部业务服务。
