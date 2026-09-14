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


def make_root(
    tmp_path: Path, deps: list[str], notices: str | None = None, npm: dict | None = None
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
    root = make_root(tmp_path, deps, notices=_notices(deps, ALWAYS), npm=npm)
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
