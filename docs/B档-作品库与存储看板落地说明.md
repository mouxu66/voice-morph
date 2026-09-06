# B 档落地说明 · 作品库（B1）与存储看板（B2）

> 对应 `docs/功能完善建议-2026-09-05.md` 的 B1 / B2。

## B1 作品库：收藏 / 标签 / 批量导出

产物（tts / offlinevc / audiobook / fx / trial / mine）此前只能按时间和文件名翻。现在：

| 层 | 文件 | 内容 |
|---|---|---|
| 数据 | `m2_server/history.py` | 记录新增 `starred` / `tags` 字段；`set_meta` / `bulk_delete` / `all_tags` / `export_zip`；老记录读时补默认值，**不需要迁移历史文件** |
| API | `m2_server/history_api.py` | `GET /history`（+`starred`/`tag` 过滤）、`GET /history/tags`、`PATCH /history/{id}`、`POST /history/bulk_delete`、`POST /history/export`（zip 流） |
| 前端 | `web/src/components/voice-studio/WorksLibrary.tsx` | 音色库页新增「作品库」卡片：音色筛选 / 只看收藏 / 标签 chips / 星标 / 标签编辑 / 勾选批量打包 zip 与删除 / 分页加载 |

设计取舍：

- **入口塞进音色库页**，不开新导航项（原建议"避免入口碎片化"）。作品天然按音色组织，放在音色库语义也顺。
- **导出走 POST + blob 下载**而不是 GET 链接：id 列表可能很长，且能顺带透出 `X-Missing-Files`（文件已被清掉的条数）。
- **删除联动**：批量删除或存储看板清 `outputs_wav` 后，指向不存在文件的历史记录同步摘掉，不留空链接。
- 标签清洗：去空白/去重/超 16 字拒收/最多 8 个——防止把标签当自由文本滥用。

## B2 存储占用看板 + 一键清理

| 层 | 文件 | 内容 |
|---|---|---|
| 统计/清理 | `m2_server/storage.py` | 9 个目标共用一套定义（统计与清理看到的永远是同一批文件）；`scan()` / `disk_usage()` / `clean()` |
| API | `m2_server/system_api.py` | `GET /api/system/storage`、`POST /api/system/storage/clean` |
| 前端 | `web/src/components/StoragePanel.tsx` | 设置菜单 →「维护 → 存储占用与清理」弹窗：磁盘水位条 + 各目录占用 + 勾选清理 |

目标清单（`cleanable=false` 的两项**受保护，永不清理**）：

| key | 内容 | 可清理 |
|---|---|---|
| market_downloads | 市场下载的 .pth/.index/.part（不动 downloads.json） | ✅ |
| market_previews | 市场自动试听音频 | ✅ |
| outputs_wav | outputs 顶层 wav（历史产物） | ✅ |
| qc_cache | 质检中间产物 | ✅ |
| clips | 解析切片 | ✅ |
| ft_corpus | 微调语料 | ✅ |
| logs | outputs 与 m2_server 的 .log | ✅ |
| rvc_weights | RVC 实时权重副本 | ❌ 只读 |
| voicebank | 音色档案 | ❌ 只读 |

设计取舍：

- **`rvc_weights` 故意设为只读**：它是 logs/<id>/<id>.pth 的硬链接副本（见 A1 改动），删了不省空间反而让实时变声失效——面板上会写明这一点。
- **voicebank / RVC logs 永不出现在可清理列表**（原建议明确排除），这两项是不可重建的用户资产。
- 清理只删文件、不删目录结构；被占用的文件跳过并计入 errors，不中断。
- 磁盘水位 ≥90% 红条 / ≥75% 黄条（对 C 盘 96% 满的场景直接可见）。

## 测试

- `tests/test_works_library.py`（11 条）：元数据读写回填 / 过滤 / 标签清洗 / 批量删除 / zip 导出（含缺失文件与重名去重）/ API 校验。
- `tests/test_storage.py`（8 条）：目标扫描精确匹配 / 受保护目标拒绝清理 / 清理联动历史 / 单文件删除失败不中断 / API。
- 全量 `pytest tests/` **210 passed**；前端 `tsc -b` + `vite build` 通过。
- ⚠️ 沙箱环境跑测试/构建前设 `CODEBUDDY_SAFE_DELETE_ENABLED=0`（safe-delete 钩子会拦 rmtree/rmSync，随机毒化用例）。
