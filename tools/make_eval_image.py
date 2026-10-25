"""生成 reply_advisor 评测用的活动安排表（2560x1600，内容完全已知，读错一眼能看出来）。

产物落在 `m2_server/tests/fixtures/reply_eval_activity.png`，跟代码一起版本化 ——
评测才能复现。需要重新生成时直接跑本脚本。

⚠️ 别把产物写到 `D:\\tmp`：2026-10-26 实测那里的文件会被系统清理，
     图和脚本一起消失，评测会静默降级成 FileNotFoundError。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 2560, 1600
img = Image.new("RGB", (W, H), (245, 246, 248))
d = ImageDraw.Draw(img)

FB = "/c/Windows/Fonts/msyhbd.ttc"
F = "/c/Windows/Fonts/msyh.ttc"
f_title = ImageFont.truetype(FB, 62)
f_h = ImageFont.truetype(FB, 40)
f_cell = ImageFont.truetype(F, 33)
f_small = ImageFont.truetype(F, 27)

d.rectangle([0, 0, W, 130], fill=(38, 51, 78))
d.text((70, 34), "十一月活动安排表（内部版）", font=f_title, fill=(255, 255, 255))
d.text((70, 145), "更新于 10 月 26 日  ·  数据来自测试服  ·  以正式服为准", font=f_small, fill=(110, 118, 132))

rows = [
    ("27 号", "巴尔德之门", "13000 秘宝兑换入场券", "入场券当日有效，过期不退", "无"),
    ("28 号", "军官计划", "23:59 前报名", "报名即领 300 钻石，逾期不补", "需等级 40"),
    ("29 号", "军备竞演", "三阶段，每阶段 12 小时", "三阶段全部完成得限定皮肤", "需 2000 战力"),
    ("30 号", "要塞争夺战", "需加入公会", "公会排名前 10 额外奖励", "需公会 3 天"),
    ("31 号", "奇味天降", "每日免费领取一次", "累计 5 次开额外宝箱", "无"),
]

x0, y0 = 70, 230
col = [200, 330, 470, 620, 300]
head = ["日期", "活动名", "参与条件", "关键奖励 / 注意事项", "其他门槛"]
y = y0
d.rectangle([x0, y, x0 + sum(col), y + 76], fill=(228, 232, 240))
cx = x0
for i, h in enumerate(head):
    d.text((cx + 18, y + 18), h, font=f_h, fill=(45, 55, 75))
    cx += col[i]
y += 76

for idx, r in enumerate(rows):
    hgt = 128
    d.rectangle([x0, y, x0 + sum(col), y + hgt],
                fill=(255, 255, 255) if idx % 2 == 0 else (250, 251, 253))
    cx = x0
    for i, cell in enumerate(r):
        color = (200, 40, 40) if (i == 3 and "皮肤" in cell) else (30, 34, 42)
        maxw = col[i] - 36
        lines, cur = [], ""
        for ch in cell:
            if d.textlength(cur + ch, font=f_cell) <= maxw:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        for li, ln in enumerate(lines[:3]):
            d.text((cx + 18, y + 30 + li * 42), ln, font=f_cell, fill=color)
        cx += col[i]
    y += hgt

y += 30
d.rectangle([x0, y, x0 + sum(col), y + 210], fill=(255, 249, 235), outline=(230, 200, 150), width=2)
notes = [
    "群主备注：",
    "1. 29 号军备竞演是唯一产出「限定皮肤」的活动，其余活动只给钻石/宝箱，别搞混。",
    "2. 28 号报名和 27 号入场券都要在当天 23:59 前操作，跨天不补。",
    "3. 30 号需要入会满 3 天，临时抱佛脚来不及。",
]
for i, n in enumerate(notes):
    d.text((x0 + 24, y + 18 + i * 48), n, font=f_cell if i == 0 else f_small,
           fill=(120, 70, 20) if i == 0 else (70, 70, 70))

out = Path(__file__).resolve().parent.parent / "m2_server" / "tests" / "fixtures" / "reply_eval_activity.png"
img.save(out, "PNG")
print("saved", out, img.size)
