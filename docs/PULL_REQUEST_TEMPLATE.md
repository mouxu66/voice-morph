# Pull Request 模板

> 复制以下内容到 PR 描述。配合 `CODE_REVIEW_GUIDE.md` / `CODE_REVIEW_PROCESS.md` 使用。

## 改动摘要
<!-- 一句话说清改了什么、为什么 -->

## 影响范围
- [ ] 后端 `m2_server`
- [ ] 推理 / RVC
- [ ] TTS 流式
- [ ] 前端 `web`
- [ ] Electron 主进程
- [ ] 移动端 `mobile`
- [ ] 仅文档/配置

## 自审结果（阶段 1 门禁）
- 后端回归：`CODEBUDDY_SAFE_DELETE_ENABLED=0 pytest m2_server -q` → [ ] 通过（绿 X/基线 272~280）
- 前端构建：`cd web && npm run build` → [ ] 通过
- 新增/更新测试：<!-- 列出用例文件 -->
- 无密钥/个人路径入库：<!-- 是/否 -->

## 验证方式
<!-- 后端贴 pytest 结果；前端贴关键交互截图或录屏链接 -->

## 审查结论
- 火眼眼初审：<!-- 无 🔴 / 已修复 N 个 🔴 -->
- 遗留 🟡（如需跟进）：<!-- 列出 + 跟进 issue -->

## 备注
<!-- 设计 RFC 链接、已知限制、需维护者拍板的点 -->
