import { useRef, useState } from "react"
import { Check, CircleAlert, Loader2, MessageSquareText, Pencil, Play, Plus, Square, Trash2, X } from "lucide-react"
import { usePhraseBoard } from "@/pages/Tts/usePhraseBoard"

/**
 * 快捷短语板：课堂高频用语一键播出（直播/录课时让嗓子歇一歇）。
 *
 * 宿主就是 `sound.tts` 自己的路由页 —— 插件被关时整页都不在，所以这里**不需要**
 * 再做跨插件门控（与 `SoundboardPanel` 被别的页托管、必须自门控的情况不同）。
 */
export function PhraseBoardCard() {
  const pb = usePhraseBoard()
  const [draft, setDraft] = useState("")
  const [editingId, setEditingId] = useState("")
  const [editText, setEditText] = useState("")
  const editInput = useRef<HTMLInputElement | null>(null)

  const startEdit = (id: string, text: string) => {
    setEditingId(id)
    setEditText(text)
    requestAnimationFrame(() => editInput.current?.focus())
  }

  const submitEdit = async () => {
    if (!editingId) return
    await pb.update(editingId, editText)
    setEditingId("")
  }

  const submitAdd = async () => {
    await pb.add(draft)
    setDraft("")
  }

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium text-primary">替我说一句</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <MessageSquareText className="h-4 w-4 text-primary" />
            快捷短语
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            点一下就用当前音色念出来（本机试听）。直播/放映中用全局热键 Ctrl+Alt+1~5
            触发前 5 条，声音直接进虚拟声卡 —— 钉钉、录屏那头听得到，不用切回本应用。
          </p>
        </div>
      </div>

      {/* 新增：一行输入 + 添加 */}
      <div className="mt-4 flex items-center gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submitAdd()
          }}
          placeholder="添加课堂常用语，如：请大家看第 5 页"
          maxLength={200}
          className="h-9 min-w-0 flex-1 rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
        />
        <button
          type="button"
          disabled={!draft.trim()}
          onClick={() => void submitAdd()}
          className="inline-flex shrink-0 items-center gap-1 rounded-md bg-primary/10 border border-primary/40 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-50"
        >
          <Plus className="h-3.5 w-3.5" />添加
        </button>
      </div>

      <ul className="mt-3 space-y-2">
        {pb.items.map((it, idx) => {
          const active = pb.activeId === it.id
          const editing = editingId === it.id
          return (
            <li
              key={it.id}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2 transition ${
                active ? "border-primary bg-primary/10" : "border-border bg-background/60"
              }`}
            >
              <button
                type="button"
                disabled={active}
                onClick={() => void pb.play(it.id)}
                title={active ? "正在念这条…" : `用当前音色念：「${it.text}」`}
                aria-label={`播放 ${it.text}`}
                className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border transition disabled:pointer-events-none ${
                  active
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20"
                }`}
              >
                {active ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              </button>
              {/* 前 5 条 = 热键目标（Ctrl+Alt+N 全局触发、播进虚拟声卡），角标要一眼可见。
                  ⚠️ 别用裸 Alt+N：菜单栏会抢（autoHideMenuBar 下按 Alt 只激活菜单），
                  且与微信「按住说话」的 Alt 打架 —— 详见 main.cjs 的 PHRASE_HOTKEY_PREFIX。 */}
              {idx < 5 && (
                <kbd
                  title={`全局热键 Ctrl+Alt+${idx + 1}：任意界面按下即播这条（进虚拟声卡）`}
                  className="hidden shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] leading-4 text-muted-foreground sm:inline-block"
                >
                  Ctrl+Alt+{idx + 1}
                </kbd>
              )}
              {editing ? (
                <>
                  <input
                    ref={editInput}
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void submitEdit()
                      if (e.key === "Escape") setEditingId("")
                    }}
                    maxLength={200}
                    className="h-8 min-w-0 flex-1 rounded-md border border-primary/40 bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  />
                  <button
                    type="button"
                    onClick={() => void submitEdit()}
                    aria-label="保存修改"
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary/40 bg-primary/10 text-primary transition hover:bg-primary/20"
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingId("")}
                    aria-label="取消修改"
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </>
              ) : (
                <>
                  <p className="min-w-0 flex-1 truncate text-sm text-card-foreground" title={it.text}>
                    {it.text}
                  </p>
                  <button
                    type="button"
                    onClick={() => startEdit(it.id, it.text)}
                    aria-label={`编辑 ${it.text}`}
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition hover:text-primary"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void pb.remove(it.id)}
                    aria-label={`删除 ${it.text}`}
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </>
              )}
            </li>
          )
        })}
        {pb.items.length === 0 && (
          <li className="rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
            还没有短语。在上面加一句，或重启后端后这里会铺好 5 条课堂常用语。
          </li>
        )}
      </ul>

      {!pb.hasVoice && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-yellow-600">
          <CircleAlert className="h-3.5 w-3.5 shrink-0" />
          先在上方选择音色（含「声音保存」的克隆音色），短语才会用那个声音念出来。
        </p>
      )}
      {pb.error && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-destructive">
          <CircleAlert className="h-3.5 w-3.5 shrink-0" />
          {pb.error}
        </p>
      )}
      {/* 播放中的「停止」：播错了能立刻掐掉，不用等它念完 */}
      {pb.activeId && (
        <button
          type="button"
          onClick={pb.stop}
          className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:border-destructive/50 hover:text-destructive"
        >
          <Square className="h-3 w-3" />停止播放
        </button>
      )}
    </div>
  )
}
