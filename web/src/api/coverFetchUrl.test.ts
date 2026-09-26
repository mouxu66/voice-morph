import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchCoverUrl, runCover, suggestCoverPitch } from "./client";

/**
 * 「粘直链」前端契约：`src_name` 必须真的进 FormData。
 *
 * 为什么值得单独钉这一条：`/cover/run` 的源是**二选一**（`file` 或 `src_name`），
 * 后端只看 `src_name` 这一根线。如果前端漏传（比如改成只在有 file 时才 append），
 * 表现是"粘了直链、点了开跑、后端说请先选择歌曲"—— 两边各自看代码都像对的。
 *
 * 另一个坑在 `fetchCoverUrl`：它是 `jsonFetch`（JSON body），不是 FormData。
 * 后端 `/cover/fetch` 收的是 pydantic 模型的 JSON body；写成 FormData 会 422，
 * 而且 422 的 detail 是数组，前端的 `detail` 取值逻辑会显示成 "[object Object]"。
 */

function stubFetch(ok = true, payload: unknown = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fn = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    return {
      ok,
      status: ok ? 200 : 400,
      statusText: ok ? "OK" : "Bad Request",
      json: async () => payload,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchCoverUrl", () => {
  it("POST 到 /cover/fetch，且用 JSON body 而不是 FormData", async () => {
    const calls = stubFetch(true, { ok: true, name: "cover_src_1.mp3", url: "/api/media/x", bytes: 3, duration_s: 1 });

    await fetchCoverUrl("https://example.com/song.mp3");

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("/cover/fetch");
    expect(calls[0].init.method).toBe("POST");

    const body = calls[0].init.body;
    // JSON body 是字符串；FormData 是对象 —— 这条断言就是"用错 Content-Type"的守门人
    expect(typeof body).toBe("string");
    expect(JSON.parse(body as string)).toEqual({ url: "https://example.com/song.mp3" });
    expect((calls[0].init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("原样回传后端的会话文件名与试听地址", async () => {
    stubFetch(true, {
      ok: true,
      name: "cover_src_7.mp3",
      url: "/api/media/session/cover_src_7.mp3",
      bytes: 1024,
      duration_s: 12.5,
    });

    const r = await fetchCoverUrl("https://example.com/a.mp3");

    expect(r.name).toBe("cover_src_7.mp3");
    expect(r.url).toBe("/api/media/session/cover_src_7.mp3");
  });
});

describe("runCover 的源二选一", () => {
  it("只给 src_name 时，FormData 里有它、没有 file", async () => {
    const calls = stubFetch();

    await runCover(null, "kangaroo", 0, 0.5, 1, 1, true, "cover_src_3.mp3");

    const form = calls[0].init.body as FormData;
    expect(form.get("src_name")).toBe("cover_src_3.mp3");
    expect(form.get("file")).toBeNull();
    expect(form.get("voice_id")).toBe("kangaroo");
  });

  it("只给 file 时，不带 src_name（避免后端收到空串去会话里找文件）", async () => {
    const calls = stubFetch();
    const f = new File([new Uint8Array([1, 2, 3])], "a.mp3", { type: "audio/mpeg" });

    await runCover(f, "kangaroo", 0, 0.5);

    const form = calls[0].init.body as FormData;
    expect(form.get("src_name")).toBeNull();
    expect(form.get("file")).toBeTruthy();
  });
});

describe("suggestCoverPitch 也认 src_name", () => {
  it("分析音域的源要和开跑用的是同一个", async () => {
    const calls = stubFetch(true, { ok: true, pitch: 3 });

    await suggestCoverPitch(null, "kangaroo", "cover_src_9.mp3");

    const form = calls[0].init.body as FormData;
    expect(calls[0].url).toContain("/cover/pitch_suggest");
    expect(form.get("src_name")).toBe("cover_src_9.mp3");
  });
});
