import { existsSync, readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import {
  KNOWN_VERDICT_CODES,
  TONE_CLASS,
  VERDICT_META,
  verdictMeta,
  verdictSummary,
} from "@/lib/qualityVerdict"

/**
 * P2-1 失败可诊断：**判定码跨端对账** + 兜底行为。
 *
 * 要防的回归只有一件事，但它很隐蔽：后端 `quality_verdict.py` 加了一个新的
 * `VERDICT_*` 常量（比如以后加 `wrong_language`），前端这张表没跟上 ——
 * 症状是**不报错**，只是新结论落到 `verdictMeta()` 的兜底分支显示成灰色。
 * 于是"该提醒用户换素材"的硬错误，看起来像一条中性说明。
 *
 * 所以这里读**真实后端源码**（不写 fixture —— 写死一份码清单只会照抄我自己的
 * 假设，后端加了码它照样绿），把 `VERDICT_<NAME> = "<code>"` 全部抠出来对账。
 *
 * 同理钉住 `TONE_CLASS` 每个 tone 都有类名：`verdictMeta` 返回了 tone，但
 * `TONE_CLASS[tone]` 缺条目时取出来是 `undefined`，在 JSX 里会渲染成
 * `className="undefined ..."` —— 页面没崩，只是没有颜色。
 */

/** 找 `m2_server`（与 crossPluginGate.test.ts 同一套：找不到就抛，绝不静默返回）。 */
function findM2Dir(): string {
  let dir = process.cwd()
  for (let i = 0; i < 4; i += 1) {
    const cand = path.resolve(dir, "m2_server")
    if (existsSync(path.join(cand, "quality_verdict.py"))) return cand
    dir = path.resolve(dir, "..")
  }
  throw new Error(`找不到 m2_server/quality_verdict.py（cwd=${process.cwd()}）`)
}

function readVerdictPy(): string {
  return readFileSync(path.join(findM2Dir(), "quality_verdict.py"), "utf8").replace(/\r\n/g, "\n")
}

/** 从后端源码抠出 `VERDICT_FOO = "bar"` 里的 "bar" 集合。 */
function backendCodes(): string[] {
  const src = readVerdictPy()
  return [...src.matchAll(/^VERDICT_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1])
}

describe("判定码与后端对账", () => {
  it("后端源码里抠得到判定码（抠不到说明正则或文件结构变了，别静默放行）", () => {
    const codes = backendCodes()
    expect(codes.length).toBeGreaterThanOrEqual(6)
    expect(codes).toContain("wrong_speaker")
  })

  it("★ 后端的每个判定码，前端 VERDICT_META 都有条目", () => {
    const missing = backendCodes().filter((c) => !(c in VERDICT_META))
    expect(
      missing,
      `后端新加了判定码 ${missing.join(", ")}，前端 lib/qualityVerdict.ts 的 VERDICT_META 没跟上 —— ` +
        `症状是不报错、只是显示成灰色兜底文案。`,
    ).toEqual([])
  })

  it("★ 前端不许有后端不认识的判定码（幽灵码会让配色表悄悄腐烂）", () => {
    const backend = new Set(backendCodes())
    const ghost = KNOWN_VERDICT_CODES.filter((c) => !backend.has(c))
    expect(ghost, `前端清单里的 ${ghost.join(", ")} 后端已不存在，请一并删掉`).toEqual([])
  })

  it("KNOWN_VERDICT_CODES 与 VERDICT_META 的键集一致（两个源会漂移）", () => {
    expect([...KNOWN_VERDICT_CODES].sort()).toEqual(Object.keys(VERDICT_META).sort())
  })
})

describe("兜底行为", () => {
  it("未知码返回 unknown 元数据而不是 undefined（取属性会崩）", () => {
    for (const bad of ["", undefined, null, "no_such_code"]) {
      const m = verdictMeta(bad as string)
      expect(m).toBeTruthy()
      expect(m.tone).toBe("muted")
    }
  })

  it("每个 tone 都有对应的类名（缺条目会渲染出 className=undefined）", () => {
    for (const meta of Object.values(VERDICT_META)) {
      const cls = TONE_CLASS[meta.tone]
      expect(cls, `tone=${meta.tone} 缺 TONE_CLASS 条目`).toBeTruthy()
      expect(cls.box).toBeTruthy()
      expect(cls.title).toBeTruthy()
      expect(cls.icon).toBeTruthy()
    }
  })

  it("只有 ok 用绿色 —— 「可用」与「可补救」必须在视觉上分开", () => {
    const okTone = VERDICT_META.ok.tone
    const others = Object.entries(VERDICT_META).filter(([k]) => k !== "ok")
    expect(others.every(([, m]) => m.tone !== okTone)).toBe(true)
  })
})

describe("verdictSummary 汇总文案", () => {
  it("没有切片时说「没有切出切片」，不出现 0% 这种误导性数字", () => {
    expect(verdictSummary({ total: 0, ok: 0 })).toBe("没有切出切片")
  })

  it("带切片时同时给出总数与可用数（用户要的是「能用几条」不是「切了几条」）", () => {
    const s = verdictSummary({ total: 40, ok: 6, ok_ratio: 0.15 })
    expect(s).toContain("40")
    expect(s).toContain("6")
    expect(s).toContain("15%")
  })

  it("ok_ratio 缺失时自己算，不显示 NaN", () => {
    const s = verdictSummary({ total: 10, ok: 5 })
    expect(s).toContain("50%")
    expect(s).not.toContain("NaN")
  })
})
