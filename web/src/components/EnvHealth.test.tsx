/**
 * `EnvHealth` 的「复制诊断报告」——2026-10-05（随 AI 调试接口第一期一起加）。
 *
 * 守两件容易悄悄退化的事：
 *  1. **优先走后端的完整报告**（`/api/ai/context?format=md`），而不是前端自己
 *     拼的简版 —— 简版只有体检项的勾叉，答不了「这次为什么失败」（能力加载 /
 *     音频链路 / 上一条发送的 warning / 日志都在完整版里，见
 *     `docs/AI调试端点设计.md`）。
 *  2. **降级要如实标注**：端点拿不到时退回简版，但按钮文案是「已复制（简版）」——
 *     一句「已复制」把两种内容不同的东西说成同一件事，会让用户以为 AI 看到了完整现场。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const getHealth = vi.fn()
const diagnose = vi.fn()
const getDiagnosticReport = vi.fn()

vi.mock("@/api/client", () => ({
  getHealth: (...a: unknown[]) => getHealth(...a),
  diagnose: (...a: unknown[]) => diagnose(...a),
  getDiagnosticReport: (...a: unknown[]) => getDiagnosticReport(...a),
}))

// `@/lib/electron` 是 IPC 门面：本用例不涉桌面端拉起，全部桩掉（hasElectron=false）
vi.mock("@/lib/electron", () => ({
  hasElectron: false,
  showBackendLog: vi.fn(),
  startBackend: vi.fn(),
  stopBackend: vi.fn(),
}))

import { useAppStore } from "@/store/useAppStore"
import { EnvHealth } from "./EnvHealth"

const HEALTH_OK = { status: "ok", cuda: true }
const DIAG_OK = { all_ok: true, items: [], skipped: [] }
const DIAG_ONE_FAIL = {
  all_ok: false,
  items: [
    { key: "ffmpeg", ok: false, label: "ffmpeg", detail: "未找到 ffmpeg", hint: "装它并加 PATH" },
  ],
  skipped: [],
}

let writeText: ReturnType<typeof vi.fn>

describe("EnvHealth · 复制诊断报告", () => {
  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    useAppStore.setState({ backendUp: true })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it("★ 优先复制后端完整报告（而不是前端简版）", async () => {
    getHealth.mockResolvedValue(HEALTH_OK)
    diagnose.mockResolvedValue(DIAG_OK)
    getDiagnosticReport.mockResolvedValue("# 变声工坊 · 诊断报告\n完整内容")

    render(<EnvHealth open onClose={() => {}} />)
    fireEvent.click(await screen.findByRole("button", { name: "复制诊断报告" }))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(getDiagnosticReport).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0][0]).toContain("完整内容")
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument()
  })

  it("★ 端点拿不到 → 降级简版，且如实标注「简版」", async () => {
    getHealth.mockResolvedValue(HEALTH_OK)
    diagnose.mockResolvedValue(DIAG_ONE_FAIL)
    getDiagnosticReport.mockRejectedValue(new Error("404 Not Found"))

    render(<EnvHealth open onClose={() => {}} />)
    fireEvent.click(await screen.findByRole("button", { name: "复制诊断报告" }))

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const text = writeText.mock.calls[0][0] as string
    expect(text).toContain("环境体检")
    expect(text).toContain("【ffmpeg】未通过")
    // 关键：不许把简版说成完整报告
    expect(await screen.findByRole("button", { name: "已复制（简版）" })).toBeInTheDocument()
  })
})
