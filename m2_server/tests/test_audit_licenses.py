"""tools/audit_licenses.py 的单元测试 —— 「许可登记不会腐烂」这件事必须机器钉住。

为什么值得单独测：`THIRD_PARTY_NOTICES.md` 的价值全在**覆盖性**上（每个声明依赖都
登记了、每条义务都在）。它的失效方式又特别安静 —— 有人加了个依赖、文档没跟上，
平时毫无症状，只有发版分发出去以后才变成**补不回来的违规**。所以判据必须是机器判的。

这个工具的两个特点决定了测法：

1. **它判"有没有漏"，不判"许可填得对不对"** —— 许可事实由人回溯一手来源（工具只读
   本地文本，不发网络请求）。所以测试构造的是"覆盖关系"，不是"真实许可值"。
2. **双向严格**：漏登记失败，**残留条目也失败**（否则文档会慢慢攒一堆"看着像在维护"
   的死条目）。两个方向都要有用例。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"


def _load(name: str):
    """按路径加载 tools/ 下的脚本（它们不是包，不能 `import`）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load("audit_licenses")


@pytest.fixture(scope="module")
def sync():
    return _load("sync_license_payload")


# ------------------------------------------------------------------ 夹具构造
PY_REQ = """\
# 注释行要忽略
-r requirements-base.txt      # 指令行要忽略
torch
torchaudio
Pillow          # 行尾注释 + 大小写
python_multipart   # 下划线写法，必须与 python-multipart 等价
requests>=2.31  # 版本约束要剥掉
numpy; python_version >= "3.11"
"""

NPM_PKG = {
    "name": "demo",
    "dependencies": {"react": "^19.0.0", "@fontsource/inter": "^5.3.0"},
    "devDependencies": {"vite": "^5.0.0", "should-not-be-listed": "^1.0.0"},
}

ALWAYS = [
    "vb-cable-terms",
    "ffmpeg-external",
    "no-nc-weights",
    "third-party-asset-names",
    "market-voice-disclaimer",
]


def _notices(deps: list[str], obligations: list[str] | None = None) -> str:
    oblig = obligations if obligations is not None else [*ALWAYS, "ofl-font-notice"]
    dep_block = "\n".join(deps)
    oblig_block = "\n".join(f"{k} = 说明" for k in oblig)
    return (
        "# 假 notices\n\n"
        f"<!-- audit:deps:begin -->\n{dep_block}\n<!-- audit:deps:end -->\n\n"
        f"<!-- audit:obligations:begin -->\n{oblig_block}\n<!-- audit:obligations:end -->\n"
    )


# ------------------------------------------------------------ 许可原文载荷夹具
#
# 载荷判据（"义务登记了" → "义务履行了"）比登记判据多一个物理层：
# 光在 notices 里写 `ofl-font-notice` 不算数，`web/public/licenses/` 里得真有原文，
# 且版权行与清单对得上。所以夹具必须能造出**一份合法的载荷**，
# 否则每条正向用例都会因为"缺载荷"而红 —— 那样测的就不是登记逻辑了。
def _payload_files(fonts: list[str]) -> dict[str, str]:
    """构造 (载荷文件名 → 内容)，清单与原文的版权行**自洽**（漂移用例另行破坏）。"""
    files: dict[str, str] = {
        "PROJECT-LICENSE.txt": "MIT License\n\nCopyright (c) 2026 demo\n"
    }
    components = [
        {
            "id": "project-license",
            "name": "demo 项目自身",
            "version": "",
            "license": "MIT",
            "copyright": "Copyright (c) 2026 demo",
            "file": "PROJECT-LICENSE.txt",
            "distributed": "随安装包",
            "scope": "本项目",
        }
    ]
    for dep in fonts:
        stem = dep.removeprefix("@fontsource/")
        fname = f"{stem}-OFL-1.1.txt"
        line = f"Copyright 2020 The {stem} Project Authors"
        files[fname] = f"{line}\n\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n"
        components.append(
            {
                "id": f"font-{stem}",
                "name": dep,
                "version": "5.3.0",
                "license": "OFL-1.1",
                "copyright": line,
                "file": fname,
                "distributed": "随安装包",
                "scope": f"{stem} 字体",
            }
        )
    files["index.json"] = json.dumps({"components": components}, ensure_ascii=False, indent=2)
    return files


def write_payload(root: Path, files: dict[str, str]) -> Path:
    dest = root / "web" / "public" / "licenses"
    dest.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (dest / name).write_text(text, encoding="utf-8")
    return dest


def make_root(
    tmp_path: Path,
    deps: list[str],
    notices: str | None = None,
    npm: dict | None = None,
    payload: dict[str, str] | bool | None = None,
) -> Path:
    (tmp_path / "requirements.txt").write_text(PY_REQ, encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "web").mkdir(exist_ok=True)
    (tmp_path / "web" / "package.json").write_text(
        json.dumps(npm if npm is not None else NPM_PKG, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text(
        notices if notices is not None else _notices(deps), encoding="utf-8"
    )
    if payload is not False:  # 传 False 表示"故意不写载荷"
        write_payload(tmp_path, payload if payload is not None else _payload_files(["@fontsource/inter"]))
    return tmp_path


#: 与夹具里的声明集完全一致的登记项
PY_DEPS = [
    "python:torch",
    "python:torchaudio",
    "python:pillow",
    "python:python-multipart",
    "python:requests",
    "python:numpy",
    "python:pytest",
]
MATCHING = [*PY_DEPS, "npm:react", "npm:@fontsource/inter"]


# ------------------------------------------------------------------ 用例
def test_ok_when_notices_match(tool, tmp_path):
    root = make_root(tmp_path, MATCHING)
    result = tool.audit(root)
    assert result["ok"], result["errors"]
    assert result["declared_count"] == 9
    assert result["missing"] == [] and result["stale"] == []


def test_missing_dep_is_reported(tool, tmp_path):
    """漏登记 = 真漏项，必须红。"""
    deps = [d for d in MATCHING if d != "python:torch"]
    root = make_root(tmp_path, deps)
    result = tool.audit(root)
    assert not result["ok"]
    assert result["missing"] == ["torch"]
    assert any("未在 notices 登记" in e for e in result["errors"])


def test_stale_entry_is_reported(tool, tmp_path):
    """登记了但已经不是依赖 → 也失败（防文档攒死条目）。"""
    root = make_root(tmp_path, [*MATCHING, "python:removed-long-ago"])
    result = tool.audit(root)
    assert not result["ok"]
    assert result["stale"] == ["removed-long-ago"]


def test_duplicate_entry_is_reported(tool, tmp_path):
    root = make_root(tmp_path, [*MATCHING, "python:torch"])
    result = tool.audit(root)
    assert not result["ok"]
    assert result["duplicates"] == ["torch"]


@pytest.mark.parametrize("dropped", ALWAYS)
def test_every_always_obligation_is_required(tool, tmp_path, dropped):
    """5 条"永远要有"的义务，少一条都得红（逐条参数化，避免只测第一条就以为全测了）。"""
    oblig = [*ALWAYS, "ofl-font-notice"]
    oblig.remove(dropped)
    root = make_root(tmp_path, MATCHING, notices=_notices(MATCHING, oblig))
    result = tool.audit(root)
    assert not result["ok"]
    assert result["missing_obligations"] == [dropped]


def test_font_dep_triggers_ofl_obligation(tool, tmp_path):
    """加了字体依赖却没登记 OFL 义务 = 少一份必须随发行物走的许可原文。"""
    root = make_root(tmp_path, MATCHING, notices=_notices(MATCHING, ALWAYS))
    result = tool.audit(root)
    assert not result["ok"]
    assert result["missing_obligations"] == ["ofl-font-notice"]


def test_no_font_dep_means_no_ofl_obligation(tool, tmp_path):
    """反向：没有字体依赖时不该强求 OFL 义务（规则由依赖触发，不是写死）。"""
    npm = {"name": "demo", "dependencies": {"react": "^19.0.0"}}
    deps = [*PY_DEPS, "npm:react"]
    root = make_root(
        tmp_path, deps, notices=_notices(deps, ALWAYS), npm=npm, payload=_payload_files([])
    )
    assert tool.audit(root)["ok"]


def test_missing_blocks_are_reported(tool, tmp_path):
    root = make_root(tmp_path, MATCHING, notices="# 只有正文，没有机器块\n")
    result = tool.audit(root)
    assert not result["ok"]
    assert result["listed_count"] == 0
    assert any("缺少依赖机器块" in e for e in result["errors"])
    assert any("缺少义务机器块" in e for e in result["errors"])


def test_entry_format_errors_are_reported(tool, tmp_path):
    """`react`（无生态前缀）与未知生态都要报出来 —— 静默丢弃会让漏项无声通过。"""
    notices = _notices([*MATCHING, "react", "cargo:serde"])
    root = make_root(tmp_path, MATCHING, notices=notices)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("格式应为" in e for e in result["errors"])
    assert any("未知生态" in e for e in result["errors"])


def test_obligation_format_error_is_reported(tool, tmp_path):
    notices = _notices(MATCHING, []).replace(
        "<!-- audit:obligations:begin -->", "<!-- audit:obligations:begin -->\n缺等号的行"
    )
    root = make_root(tmp_path, MATCHING, notices=notices)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("义务条目格式" in e for e in result["errors"])


def test_missing_notices_file(tool, tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    result = tool.audit(tmp_path)
    assert not result["ok"]
    assert result["declared_count"] == 1


# ------------------------------------------------ 第二层：许可原文载荷（"做了没"）
#
# 登记判据只证明"有人知道该附原文"，不证明"真附了"。这一组测的是物理层。
def test_missing_payload_is_reported(tool, tmp_path):
    """载荷整个缺失 → 红。这是 G1 那种"只打包了 woff2、没附 OFL 原文"的形态。"""
    root = make_root(tmp_path, MATCHING, payload=False)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("缺少许可原文载荷" in e for e in result["errors"])
    assert result["payload_components"] == 0


def test_font_dep_without_payload_entry_is_reported(tool, tmp_path):
    """字体依赖有、载荷里没有 → 红（OFL-1.1 §1 是硬要求，不是可选项）。"""
    payload = _payload_files([])  # 故意漏掉 @fontsource/inter
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("@fontsource/inter" in e and "没有对应的许可原文载荷" in e for e in result["errors"])


def test_payload_entry_for_removed_font_is_reported(tool, tmp_path):
    """载荷里留着已删包的许可原文 → 也红（否则发行物里躺着无关许可，读者会误判清单是活的）。"""
    npm = {"name": "demo", "dependencies": {"react": "^19.0.0"}}
    deps = [*PY_DEPS, "npm:react"]
    stale_payload = _payload_files(["@fontsource/inter"])  # inter 已不是依赖
    root = make_root(tmp_path, deps, notices=_notices(deps, ALWAYS), npm=npm, payload=stale_payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("已不是声明依赖（残留）" in e for e in result["errors"])


def test_payload_declared_file_missing_is_reported(tool, tmp_path):
    """清单里声明了原文文件，但文件不在 → 红（清单不能自己给自己作证）。"""
    payload = _payload_files(["@fontsource/inter"])
    del payload["inter-OFL-1.1.txt"]
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("载荷声明的原文不存在" in e for e in result["errors"])


def test_payload_copyright_drift_is_reported(tool, tmp_path):
    """**最难查的漂移**：原文在、清单在，但清单声明的版权行与原文里的对不上。

    这正是"把版权行手写进文档"必然产生的腐烂 —— 上游换了版权行，没人会去改文档。
    所以本轮改成版权行从原文正则提取、由 sync 工具生成，并用这条测试钉住。
    """
    payload = _payload_files(["@fontsource/inter"])
    manifest = json.loads(payload["index.json"])
    for comp in manifest["components"]:
        if comp["name"] == "@fontsource/inter":
            comp["copyright"] = "Copyright 1999 Nobody"  # 单改清单，原文不动
    payload["index.json"] = json.dumps(manifest, ensure_ascii=False)
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("找不到声明的版权行" in e for e in result["errors"])


def test_empty_payload_file_is_reported(tool, tmp_path):
    payload = _payload_files(["@fontsource/inter"])
    payload["inter-OFL-1.1.txt"] = "   \n"
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("空文件" in e for e in result["errors"])


def test_payload_manifest_must_be_valid_json(tool, tmp_path):
    payload = _payload_files(["@fontsource/inter"])
    payload["index.json"] = "{ 这不是 JSON"
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("不是合法 JSON" in e for e in result["errors"])


def test_payload_manifest_component_must_have_required_fields(tool, tmp_path):
    payload = _payload_files(["@fontsource/inter"])
    manifest = json.loads(payload["index.json"])
    del manifest["components"][1]["license"]  # 字体那条缺 license
    payload["index.json"] = json.dumps(manifest, ensure_ascii=False)
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert not result["ok"]
    assert any("缺少 `license`" in e for e in result["errors"])


# ------------------------------------------------ 第三层：资产触发（不许凭空要求）
def _put_asset(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_asset_without_attribution_obligation_is_reported(tool, tmp_path):
    """发行物里真出现了 openmoji 图标却没登记署名义务 → 红。"""
    root = make_root(tmp_path, MATCHING)
    _put_asset(root, "m2_server/assets/market_imgs/openmoji-rocket.png")
    result = tool.audit(root)
    assert not result["ok"]
    assert result["asset_triggers"] == ["openmoji"]
    assert any("openmoji-attribution" in e for e in result["errors"])


def test_asset_trigger_is_satisfied_by_obligation_line(tool, tmp_path):
    """登记了署名义务且资产确实存在 → 绿。"""
    root = make_root(tmp_path, MATCHING, notices=_notices(MATCHING, [*ALWAYS, "ofl-font-notice", "openmoji-attribution"]))
    _put_asset(root, "m2_server/assets/market_imgs/openmoji-rocket.png")
    assert tool.audit(root)["ok"], tool.audit(root)["errors"]


def test_no_asset_means_no_obligation_required(tool, tmp_path):
    """**phantom obligation 的护栏**：资产不存在时不该要求署名。

    2026-09-14 实证：NOTICES 曾把 "市场占位图 = OpenMoji" 列为分发组件并挂署名缺口，
    而 `git log --all --diff-filter=A -- '*openmoji*'` 为空 —— 义务只源自一句描述意图
    的代码注释。这条测试保证"没有产物就不许凭空要求"，否则手写断言能无限制造工作量。
    """
    root = make_root(tmp_path, MATCHING)
    result = tool.audit(root)
    assert result["ok"], result["errors"]
    assert result["asset_triggers"] == []


def test_attribution_text_in_payload_does_not_trigger_asset_rule(tool, tmp_path):
    """署名文件本身是**补救措施**，不能自命中变成违规证据 —— 否则这条规则一加就废。"""
    payload = _payload_files(["@fontsource/inter"])
    payload["openmoji-CC-BY-SA-4.0.txt"] = "CC BY-SA 4.0 (c) hfg-gmuend/openmoji\n"
    root = make_root(tmp_path, MATCHING, payload=payload)
    result = tool.audit(root)
    assert result["asset_triggers"] == []
    assert result["ok"], result["errors"]


def test_scan_assets_ignores_non_asset_extensions(tool, tmp_path):
    """只有素材后缀才算资产；`.md`/`.py` 里提到 openmoji 不构成分发义务。"""
    (tmp_path / "web" / "public").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "public" / "openmoji-notes.md").write_text("openmoji", encoding="utf-8")
    assert tool.scan_assets(tmp_path, "openmoji") == []


# ------------------------------------------------ 第四层：分发面权重（红线 §4.1）
#
# 红线 §4.1 原本只是一句话。把 G5/G6 核完之后它有了具体名字：
# demucs 权重训练自 MUSDB18（学术）、RVC 底模条款写"仅供研究使用"。
# 两者都是"看着能打包、实际不能"，所以改成"打包面出现权重文件就红"。
BUILD_PKG = {
    "name": "demo",
    "dependencies": {"react": "^19.0.0", "@fontsource/inter": "^5.3.0"},
    "build": {
        "files": ["dist/**/*", "electron/**/*"],
        "extraResources": [
            {"from": "dist", "to": "backend/web_dist"},
            {"from": "../m2_server", "to": "backend/m2_server"},
        ],
    },
}


def test_packaged_dirs_follow_build_config(tool, tmp_path):
    """`files` 的 glob 取 `*` 前的基目录；`extraResources.from` 按 `web/` 解析。"""
    root = make_root(tmp_path, MATCHING, npm=BUILD_PKG)
    (root / "web" / "dist").mkdir(parents=True)
    (root / "web" / "electron").mkdir(parents=True)
    (root / "m2_server").mkdir()  # 目录不存在时会被跳过（宁可少报也不误报）
    names = {raw for raw, _p in tool.packaged_dirs(root)}
    assert names == {"dist", "electron", "../m2_server"}


def test_packaged_weight_is_reported(tool, tmp_path):
    """把权重塞进打包目录 → 红。这是"顺手把 tts_models 加进 extraResources"的形态。"""
    root = make_root(tmp_path, MATCHING, npm=BUILD_PKG)
    (root / "m2_server").mkdir(exist_ok=True)
    (root / "m2_server" / "rmvpe.pt").write_bytes(b"\x00")
    result = tool.audit(root)
    assert not result["ok"]
    assert result["packaged_weights"] == ["m2_server/rmvpe.pt"]
    assert any("红线 §4.1" in e for e in result["errors"])


@pytest.mark.parametrize("rel", [
    "m2_server/__pycache__/hubert_base.pt",
    "m2_server/tests/fixture.pth",
    "m2_server/data/cache.onnx",
])
def test_excluded_parts_do_not_trigger(tool, tmp_path, rel):
    """`__pycache__` / `tests` / `data` 本来就不进包（filter 排除了），不该误报。

    误报的代价很实：这条规则一旦开始喊狼来了，下一次真出事就没人看它。
    """
    root = make_root(tmp_path, MATCHING, npm=BUILD_PKG)
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00")
    assert tool.audit(root)["packaged_weights"] == []


def test_archive_is_not_treated_as_weight(tool, tmp_path):
    """`.zip` 不进权重后缀表 —— 压缩包可能是合法素材，宁可漏报也不误报。"""
    root = make_root(tmp_path, MATCHING, npm=BUILD_PKG)
    (root / "m2_server").mkdir(exist_ok=True)
    (root / "m2_server" / "assets.zip").write_bytes(b"PK\x03\x04")
    assert tool.audit(root)["packaged_weights"] == []


def test_outside_repo_packaging_is_ignored(tool, tmp_path):
    """`from` 指到仓库外（如 `D:/RVC`）时不解析 —— 本工具只对本仓库判据负责。"""
    npm = dict(BUILD_PKG)
    npm["build"] = {"extraResources": [{"from": "../../elsewhere", "to": "x"}]}
    root = make_root(tmp_path, MATCHING, npm=npm)
    assert tool.packaged_dirs(root) == []


def test_repo_ships_no_model_weights(tool):
    """**红线 §4.1 的当前基线**：发行物里 0 个权重文件。

    这条测试变红意味着有人把底模/权重放进了打包面 —— 那时候先去看
    `THIRD_PARTY_NOTICES.md` §2 的 demucs 与 RVC 两行，别急着改测试。
    """
    weights = tool.scan_forbidden_weights(ROOT)
    assert weights == [], f"发行物里出现了权重：{weights}"
    assert [raw for raw, _p in tool.packaged_dirs(ROOT)], "推不出打包目录（构建配置变了？）"


# ------------------------------------------------------------------ 解析
def test_requirements_parsing(tool, tmp_path):
    """注释 / `-r` 指令 / 版本约束 / 环境标记 / 行尾注释都要剥干净。"""
    (tmp_path / "requirements.txt").write_text(PY_REQ, encoding="utf-8")
    names = tool.parse_requirements(tmp_path / "requirements.txt")
    assert names == ["torch", "torchaudio", "pillow", "python-multipart", "requests", "numpy"]
    assert not any(n.startswith("-") for n in names)


def test_name_normalization(tool):
    """归一化的等价关系 —— 写错一个字符就会被判成"漏项"，这条防止假红。"""
    assert tool.normalize("Pillow") == "pillow"
    assert tool.normalize("python_multipart") == "python-multipart"
    assert tool.normalize(" @FontSource/Inter ") == "@FontSource/Inter"  # scoped 保留原样


def test_dev_dependencies_are_not_declared(tool, tmp_path):
    """devDependencies 不进发行物，不该出现在声明集里。"""
    root = make_root(tmp_path, MATCHING)
    declared = tool.audit(root)
    assert declared["declared_count"] == 9  # 少了 vite / should-not-be-listed 正好说明没进去


def test_missing_package_json_is_tolerated(tool, tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "THIRD_PARTY_NOTICES.md").write_text(_notices(["python:requests"]), encoding="utf-8")
    write_payload(tmp_path, _payload_files([]))
    assert tool.audit(tmp_path)["ok"]


# ------------------------------------------------------------------ CLI / 真实仓库
def test_main_exit_code_ok(tool, tmp_path, capsys):
    root = make_root(tmp_path, MATCHING)
    assert tool.main(["--root", str(root)]) == 0
    assert "RESULT: OK" in capsys.readouterr().out


def test_main_exit_code_fail_and_json(tool, tmp_path, capsys):
    deps = [d for d in MATCHING if d != "python:torch"]
    root = make_root(tmp_path, deps)
    assert tool.main(["--root", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["missing"] == ["torch"]


def test_repo_itself_is_green(tool):
    """**最重要的一条**：把"本仓库当前是绿的"钉住。

    它会在别人给 requirements 加包却没改 notices 时立刻变红 —— 这正是这个工具存在的理由。
    失败时先改 `THIRD_PARTY_NOTICES.md`，别改这条测试。
    """
    result = tool.audit(ROOT)
    assert result["ok"], result["errors"]
    assert result["declared_count"] >= 30
    assert (ROOT / "THIRD_PARTY_NOTICES.md").exists()


def test_repo_ships_font_license_payload(tool):
    """G1 的验收条件：三份 OFL 原文必须**真在**发行物路径里，且版权行来自原文。"""
    result = tool.audit(ROOT)
    assert result["payload_components"] >= 4, result["errors"]
    for stem in ("inter", "outfit", "jetbrains-mono"):
        f = ROOT / "web" / "public" / "licenses" / f"{stem}-OFL-1.1.txt"
        assert f.is_file(), f"缺 {f}"
        text = f.read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text
        assert "Copyright" in text.splitlines()[0]


def test_repo_has_no_phantom_openmoji_obligation(tool):
    """G2 的验收条件：发行物里**没有** openmoji 资产 → 就不该要求署名义务。

    这条测试的作用是双向的：将来真引入了 openmoji 图标，它会变红并强制补署名；
    而如果有人重新写一条"网络搜集图"的义务却拿不出产物，这里不会被糊过去。
    """
    assert tool.scan_assets(ROOT, "openmoji") == []
    assert tool.audit(ROOT)["asset_triggers"] == []


def test_payload_versions_match_lockfile(tool):
    """载荷里的版本号必须与入库的 lockfile 一致 —— 否则升级依赖后会留下过期许可。"""
    locked = tool.locked_versions(ROOT)
    assert locked.get("@fontsource/inter"), "lockfile 里读不到字体版本（结构变了？）"
    result = tool.audit(ROOT)
    assert not any("lockfile 是" in e for e in result["errors"]), result["errors"]


# ------------------------------------------------------------------ 载荷生成器
def _make_sync_root(tmp_path: Path) -> Path:
    """造一个 sync 工具能跑的最小仓库：root LICENSE + node_modules 里的字体。"""
    (tmp_path / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 demo\n", encoding="utf-8")
    nm = tmp_path / "web" / "node_modules"
    for pkg, stem, year in (
        ("@fontsource/inter", "inter", 2016),
        ("@fontsource/outfit", "outfit", 2021),
        ("@fontsource/jetbrains-mono", "jetbrains-mono", 2020),
    ):
        d = nm / pkg
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"name": pkg, "version": "5.3.0"}), encoding="utf-8")
        (d / "LICENSE").write_text(
            f"Copyright {year} The {stem} Project Authors (https://example.invalid)\n\n"
            "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n",
            encoding="utf-8",
        )
    return tmp_path


def test_sync_writes_payload_then_check_passes(sync, tmp_path):
    """生成 → 自检通过（生成器与校验器必须自洽，否则每次都要人工判断谁对）。"""
    root = _make_sync_root(tmp_path)
    assert sync.run(root, check=True) == 1  # 还没生成 → 漂移
    assert sync.run(root, check=False) == 0
    assert sync.run(root, check=True) == 0
    manifest = json.loads((root / "web" / "public" / "licenses" / "index.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in manifest["components"]}
    assert "@fontsource/inter" in names
    assert manifest["components"][0]["copyright"].startswith("Copyright")


def test_sync_check_detects_hand_edited_text(sync, tmp_path):
    """有人手改载荷原文 → `--check` 必须发现（否则"请勿手改"只是句空话）。"""
    root = _make_sync_root(tmp_path)
    sync.run(root, check=False)
    target = root / "web" / "public" / "licenses" / "inter-OFL-1.1.txt"
    target.write_text("被我改坏了\n", encoding="utf-8")
    assert sync.run(root, check=True) == 1
    assert sync.run(root, check=False) == 0
    assert "SIL Open Font License" in target.read_text(encoding="utf-8")


def test_sync_removes_stray_payload_files(sync, tmp_path):
    """包已删但原文还躺在发行物里 → 生成时清掉（否则许可清单会慢慢变成考古现场）。"""
    root = _make_sync_root(tmp_path)
    sync.run(root, check=False)
    stray = root / "web" / "public" / "licenses" / "removed-font-OFL-1.1.txt"
    stray.write_text("旧许可\n", encoding="utf-8")
    assert sync.run(root, check=True) == 1
    assert sync.run(root, check=False) == 0
    assert not stray.exists()


def test_sync_reports_missing_node_modules(sync, tmp_path):
    """缺 node_modules 时说清"先 npm ci"，而不是抛个 FileNotFoundError 堆栈。"""
    (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
    assert sync.run(tmp_path, check=False) == 1


def test_sync_refuses_to_invent_copyright_line(sync, tmp_path):
    """OFL 原文里提不到版权行时报错，**绝不编一个** —— 编出来的版权行比没有更糟。"""
    root = _make_sync_root(tmp_path)
    (root / "web" / "node_modules" / "@fontsource" / "inter" / "LICENSE").write_text(
        "没有版权行的一段话\n", encoding="utf-8"
    )
    assert sync.run(root, check=False) == 1
    assert not (root / "web" / "public" / "licenses").exists()


def test_drift_description_names_crlf_as_the_cause(sync):
    """行尾差异要**指名道姓**说是 CRLF 检出，而不是报成"内容不一致"。

    本仓库 core.autocrlf=true，载荷若被检出成 CRLF，每次 `--check` 都会假红；
    真正的"有人手改了原文"就会淹没在这片噪声里。所以这条描述本身就是诊断信息。
    """
    text = "Copyright 2026 X\nSIL OPEN FONT LICENSE\n"
    lf = text.encode("utf-8")
    assert "缺失" in sync._describe_drift(None, lf)
    assert "仅行尾不同" in sync._describe_drift(lf.replace(b"\n", b"\r\n"), lf)
    assert "不一致" in sync._describe_drift(b"totally different", lf)


def test_payload_files_are_byte_stable_across_checkout(sync):
    """许可原文必须**字节稳定**：`.gitattributes` 里 `web/public/licenses/** -text`。

    为什么值得测：`--check` 按字节比对。这条属性掉了以后，行为取决于各人的
    `core.autocrlf`——CI 绿、同事机器红，是那种要查一整天的失败。
    实测（2026-09-14）`-text` 在本机有效：blob 与检出都是 LF，4477 字节一致。
    """
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "web/public/licenses/** -text" in attrs
    for f in (ROOT / "web" / "public" / "licenses").glob("*.txt"):
        assert b"\r\n" not in f.read_bytes(), f"{f.name} 里混进了 CRLF"
