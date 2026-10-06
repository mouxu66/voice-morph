import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { AiDebugPanel } from "@/components/AiDebugPanel"
import { getAiActions, getAiAudit } from "@/api/client"

vi.mock("@/api/client", () => ({
  getAiActions: vi.fn(),
  getAiAudit: vi.fn(),
}))

const closedCatalog = {
  schema: 1,
  write_enabled: false,
  write_reason: "缺 VM_AI_CONTROL=1（总开关）；缺 VM_AI_TOKEN",
  missing_gates: ["缺 VM_AI_CONTROL=1（总开关）", "缺 VM_AI_TOKEN"],
  actions: [],
  denied: [
    { id: "shell.exec", reason: "红线：任何形态的任意命令/脚本执行都不做" },
    { id: "wechat.send_text", reason: "不可逆的外部副作用，要人确认" },
  ],
}

const openCatalog = {
  schema: 1,
  write_enabled: true,
  write_reason: "写动作三重门全部满足",
  missing_gates: [],
  actions: [
    { id: "plugins.enable", desc: "启用/关闭一个能力", risk: "low", schema: { id: "string", enabled: "boolean" } },
  ],
  denied: [{ id: "shell.exec", reason: "红线" }],
}

const auditEntry = {
  id: "abc123",
  ts: 1760000000,
  action: "plugins.enable",
  params: { id: "sound.watermark", enabled: false },
  dry_run: false,
  ok: true,
  code: 200,
  duration_ms: 12,
  actor: "tester",
  evidence: { plugins_disabled: ["sound.watermark"] },
  message: "已关闭 sound.watermark（重启桌面端后生效）",
}

beforeEach(() => {
  vi.mocked(getAiActions).mockReset()
  vi.mocked(getAiAudit).mockReset()
  vi.mocked(getAiActions).mockResolvedValue(closedCatalog)
  vi.mocked(getAiAudit).mockResolvedValue({ schema: 1, items: [], total: 0, source: "x", note: "还没有 AI 操作记录" })
})

describe("AiDebugPanel", () => {
  it("写能力关着时逐条点名缺哪几道门，而不是笼统说未启用", async () => {
    render(<AiDebugPanel open onClose={() => {}} />)
    // 三重门缺哪一关要能被用户读出来照着开 —— 一句「未启用」等于让人猜。
    // 用 getAllByText：write_reason 与 missing_gates 列表里都会出现这两项
    // （**这是有意的冗余** —— 一处在句子、一处在可勾选的清单里），
    // 所以断言「至少出现在清单里」，而不是「恰好出现一次」。
    await waitFor(() => expect(getAiActions).toHaveBeenCalled())
    expect((await screen.findAllByText(/VM_AI_CONTROL/)).length).toBeGreaterThan(0)
    expect((await screen.findAllByText(/VM_AI_TOKEN/)).length).toBeGreaterThan(0)
    expect(await screen.findByText("缺 VM_AI_CONTROL=1（总开关）")).toBeInTheDocument()
    expect(screen.getByText("缺 VM_AI_TOKEN")).toBeInTheDocument()
  })

  it("拒绝清单任何时候都展示（它是设计承诺，不是运行时状态）", async () => {
    render(<AiDebugPanel open onClose={() => {}} />)
    expect(await screen.findByText("shell.exec")).toBeInTheDocument()
    expect(screen.getByText(/红线/)).toBeInTheDocument()
  })

  it("可用动作带 schema —— AI 不知道怎么调就没法调", async () => {
    vi.mocked(getAiActions).mockResolvedValue(openCatalog)
    render(<AiDebugPanel open onClose={() => {}} />)
    expect(await screen.findByText("plugins.enable")).toBeInTheDocument()
    expect(screen.getByText("启用/关闭一个能力")).toBeInTheDocument()
    expect(screen.getByText("风险 low")).toBeInTheDocument()
  })

  it("审计记录展示 actor/耗时/dry-run 标记", async () => {
    vi.mocked(getAiAudit).mockResolvedValue({
      schema: 1,
      items: [auditEntry],
      total: 1,
      source: "outputs/ai_actions.jsonl",
      note: "审计文件与作品库分开保存",
    })
    const { container } = render(<AiDebugPanel open onClose={() => {}} />)
    expect(await screen.findByText(/已关闭 sound.watermark/)).toBeInTheDocument()
    // actor 与耗时挤在同一个 <p> 里（`… · code 200 · 12ms · tester`），
    // 所以按整行的正则查，而不是按单词 —— 拆成单词查会在重构换行时假红。
    const meta = container.ownerDocument.body.textContent ?? ""
    expect(meta).toMatch(/code\s*200/)
    expect(meta).toMatch(/12ms/)
    expect(meta).toMatch(/tester/)
  })

  it("dry-run 记录要有标记 —— 试算与真执行混在一起就分不清了", async () => {
    vi.mocked(getAiAudit).mockResolvedValue({
      schema: 1,
      items: [{ ...auditEntry, dry_run: true, message: "将会关闭 sound.watermark（重启后生效）" }],
      total: 1,
      source: "x",
      note: "",
    })
    render(<AiDebugPanel open onClose={() => {}} />)
    expect(await screen.findByText("dry-run")).toBeInTheDocument()
  })

  it("★ 后端取不到时清空旧数据，不留上一次的结果冒充当前状态", async () => {
    vi.mocked(getAiAudit).mockResolvedValue({
      schema: 1, items: [auditEntry], total: 1, source: "x", note: "",
    })
    const { rerender } = render(<AiDebugPanel open onClose={() => {}} />)
    expect(await screen.findByText(/已关闭 sound.watermark/)).toBeInTheDocument()

    // 后端挂了：报错 + 清空。若保留旧记录，用户会把上一轮的结果当成现在的
    vi.mocked(getAiActions).mockRejectedValue(new Error("后端未启动"))
    vi.mocked(getAiAudit).mockRejectedValue(new Error("后端未启动"))
    rerender(<AiDebugPanel open onClose={() => {}} />)
    fireEvent.click(screen.getByRole("button", { name: "刷新" }))

    await waitFor(() => {
      expect(screen.getByText(/取不到 AI 调试面/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/已关闭 sound.watermark/)).not.toBeInTheDocument()
  })

  it("close=false 时不渲染任何东西", () => {
    const { container } = render(<AiDebugPanel open={false} onClose={() => {}} />)
    expect(container.firstChild).toBeNull()
    expect(getAiActions).not.toHaveBeenCalled()
  })
})