# MuvyoSubscribeRouter（MoviePilot Neo 插件）

监听 MoviePilot Neo 内部所有新增订阅（`EventType.SubscribeAdded`，覆盖 Web UI、EmbyHub、Telegram、豆瓣同步及 API），对**已完结剧集**（及可选**电影**）优先通过外置 **Muvyo / MV3** 执行盘搜、115 转存至独立 staging、内封中文字幕门禁探测、云盘整理与 STRM 生成；若连载中则直接放行，若盘搜无果或外部服务异常则自动恢复订阅无缝回退 PT。

## 工作原理

1. **事件监听与保守完结判定**
   - 监听 `EventType.SubscribeAdded`。
   - 电视剧结合 `mediainfo`（若 `mediainfo` 缺失或缺少 `status`/目标季信息，则通过 `TmdbChain` 兜底补全）检查 `status`、`next_episode_to_air`、`season_info`、`last_air_date` 与预期集数：
     - 连载中（`Returning Series` / `In Production` / `Planned`）的**当前季/未来季**、存在待播集或状态未知时直接放行，保持 `state='N'` 走 PT 追更。
     - 剧集已完结（`Ended` / `Canceled`），或连载剧的**已播完历史季**（默认允许，且后续季已开播、预期集数明确）进入分流。
   - 默认跳过洗版（`best_version`）和多版本订阅（`version_rules`），可按用户名白名单/黑名单过滤。
2. **防竞态暂停与后台异步执行**
   - 命中条件后**同步**调用 `SubscribeOper().update(subscribe_id, {"state": "S"})` 暂停订阅并加 `subscribe_id` 去重锁，阻止 MoviePilot 定时搜索并发抢下 PT。
   - 后续流程在后台线程池执行；若提交失败、搜索无果或发生异常，自动恢复 `state='N'` 无缝回退 PT。
3. **盘搜、筛选与 115 Staging 隔离接收**
   - 调用 `POST /api/v1/resource-search/search` → `POST /api/v1/share-transfer/parse` → `POST /api/v1/share-transfer/browse`（支持有界季目录/单目录自动下钻）。
   - 校验季号与集数完整性，自动排除 `.nfo/.jpg/.jpeg/.png/.webp` 元数据旁挂。
   - 先通过 `POST /api/v1/files/115/folder` 逐级确保独立子目录（如 `/未整理/mp-router/<标题>_tmdb<id>_s<季>_<run_id>`），再调用 `POST /api/v1/share-transfer/receive` 接收视频和字幕旁挂。
4. **内封中文字幕门禁（外挂字幕不可替代）**
   - 浏览 staging 目录拿到视频的 `file_id` 与内存态 `pickcode`，构造临时 `/redirect` URL 交由 `ffprobe` 探测内封音频流与字幕流（绝不写入日志、通知或持久化数据）。
   - `auto`（默认）：外语音轨每集须含内封中文字幕；中文/国语/粤语音轨可豁免并记录诊断。
   - `required`：每集必须含内封中文字幕。
   - **门禁不通过或 `ffprobe` 缺失**：立即停止在 staging 等待人工复核，**严禁整理、严禁生成 STRM、严禁自动删除已接收的 staging 文件**。
5. **云盘整理、STRM 生成与订阅收口**
   - 调用 `POST /api/v1/organize/transfer`（强制 `copy_non_media=False`），可选调用 `POST /api/v1/strm/generate`（强制 `organize=False`）。
   - 全部成功后按配置写入 `SubscribeHistory` 并删除原订阅（派发 `EventType.SubscribeComplete`），或仅保留暂停状态。

## 关键配置项

| 配置项 | 说明 | 示例 / 默认值 |
| --- | --- | --- |
| `enabled` | 启用插件 | `false` |
| `mv_base_url` | 外置 Muvyo / MV3 服务地址 | `http://mv3.example.local:7811` |
| `mv_api_key` | Muvyo API Key / Token | 空 |
| `mv_auth_scheme` | 鉴权头格式 | `x-api-key` / `bearer` |
| `mv_storage` | 115 存储标识 | `115-default` |
| `staging_root` | 115 staging 根目录 | `/未整理/mp-router` |
| `cloud_organize_root` | 云盘整理根目录（不得为 STRM 路径，不得追加 `/series`） | `/已整理` |
| `mv3_strm_root` | 明确 STRM 侧路径（裸 `/series` 会被拒绝） | `/strm` |
| `subtitle_policy` | 内封中文字幕门禁策略 | `auto` / `required` |
| `on_subtitle_gate_fail` | 字幕门禁失败时的订阅动作（不删云盘文件） | `keep_paused` / `fallback_pt` |
| `on_success_action` | 分流成功后订阅动作 | `complete` / `pause` |
| `dry_run` | 仅执行到盘搜与分享预览，不落云盘 | `false` |

## 安全边界

- **零敏感信息泄漏**：日志、通知、持久化历史、API 返回及异常文案均经 `redaction.py` 脱敏，不输出 `mv_api_key`、`token`、`cookie`、`share_code`、`receive_code`、`pickcode` 或带鉴权直链。
- **路径守卫**：拒绝云盘整理根与 STRM 根混用，拒绝重复追加 `/series`/`/movies`，拒绝裸 `/series` 作为 STRM 根。
- **非破坏性**：插件绝不删除 qBittorrent 任务、本地硬链接、源盘或云盘 staging 文件，不直接操作 Emby 数据库。

## 安装到 MoviePilot Neo

1. **方式一（推荐，通过插件市场源安装）**：将公开插件仓库 `https://github.com/lml249/muvyo-subscribe-router` 添加到 MoviePilot Neo 的 `PLUGIN_MARKET` 环境变量（多个仓库以英文逗号分隔），刷新插件市场后即可直接搜索安装 **Muvyo 完结订阅分流**。
2. **方式二（手动复制目录）**：将 `plugins.v2/muvyosubscriberouter/` 目录复制到 MoviePilot Neo 插件目录，重载或重启 MoviePilot Neo 插件后在插件管理中启用 **Muvyo 完结订阅分流**。
3. 填写 `mv_base_url`、`mv_api_key` 与路径配置后，先通过只读接口 `GET /api/v1/plugin/MuvyoSubscribeRouter/health` 验证连通性与路径守卫。
4. 可开启 `dry_run` 或调用 `POST /api/v1/plugin/MuvyoSubscribeRouter/dry-run` 进行只读试跑，确认无误后再关闭 `dry_run` 投入使用。

## 本地运行单元测试

```bash
python3 -m unittest discover -s tests -v
# 若环境已安装 pytest，也可运行：
python3 -m pytest -v tests
```
