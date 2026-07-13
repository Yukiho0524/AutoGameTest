"""Generate QA TestCase workbooks from planning documents."""
from __future__ import annotations

import base64
import html
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TESTCASE_DIR = ROOT / "TestCase"
INPUT_DIR = TESTCASE_DIR / "_input"
UPLOAD_DIR = INPUT_DIR / "uploads"
SPEC_PATH = ROOT / "TESTCASE_SPEC.md"
SUPPORTED_EXTS = {".docx", ".pdf", ".xlsx", ".xlsm", ".txt", ".md"}
VALID_TYPES = {"顯示確認", "操作確認", "數值確認"}
VALID_PRIORITIES = {"P0", "P1", "P2"}
VALID_AUTOMATION = {"A", "S", "M"}


def _safe_stem(value: str) -> str:
    stem = Path(value or "企劃書").stem
    return re.sub(r'[\\/:*?"<>|]+', "_", stem).strip() or "企劃書"


def _safe_name(value: str) -> str:
    name = Path(value or "planning.txt").name
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name or "planning.txt"


def safe_name(value: str) -> str:
    return _safe_name(value)


def _clip(text: str, limit: int = 120_000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[內容過長，已截斷 {len(text) - limit} 字元]"


def ensure_dirs() -> None:
    TESTCASE_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def save_upload(filename: str, content_base64: str) -> Path:
    ensure_dirs()
    safe = _safe_name(filename)
    ext = Path(safe).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支援的企劃書格式：{ext or '(無副檔名)'}")
    data = base64.b64decode(content_base64 or "", validate=True)
    if not data:
        raise ValueError("上傳檔案是空的")
    out = UPLOAD_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}"
    out.write_bytes(data)
    return out


def extract_doc_text(path: str | os.PathLike) -> tuple[str, str]:
    """Return (text, mode)."""
    doc = Path(path)
    ext = doc.suffix.lower()
    if ext in (".txt", ".md"):
        return doc.read_text(encoding="utf-8", errors="replace"), "text"
    if ext == ".docx":
        return _extract_docx_text(doc), "docx-text"
    if ext in (".xlsx", ".xlsm"):
        return _extract_xlsx_text(doc), "xlsx-text"
    if ext == ".pdf":
        text = _extract_pdf_text(doc)
        if text.strip():
            return text, "pdf-text"
        return (
            "無法抽取 PDF 文字。請安裝 pdftotext，或改上傳 txt/md/docx/xlsx，"
            "或先將企劃書另存成可複製文字的 PDF。",
            "pdf-unreadable",
        )
    try:
        return doc.read_text(encoding="utf-8", errors="replace"), "text"
    except OSError:
        return "", "unknown"


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    xml = re.sub(r"</w:tc>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def _extract_xlsx_text(path: Path) -> str:
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("缺少 openpyxl，無法讀取 xlsx 或寫出 TestCase xlsx") from e
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    lines: list[str] = []
    try:
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue
            lines.append(f"===== 工作表：{sheet} =====")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    lines.append("\t".join(cells))
    finally:
        wb.close()
    return "\n".join(lines)


def _extract_pdf_text(path: Path) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def build_prompt(doc_name: str, doc_text: str, mode: str) -> str:
    spec = SPEC_PATH.read_text(encoding="utf-8", errors="replace")
    return f"""你是遊戲 QA 測試設計專家。請根據「測試案例撰寫規範」與企劃書內容，產出 QA TestCase。

# 測試案例撰寫規範
{spec}

# 企劃書
- 檔名：{doc_name}
- 讀取模式：{mode}

```text
{_clip(doc_text)}
```

# 重要原則
- 只根據企劃書明確文字撰寫 CASE。
- 企劃書沒寫清楚、PDF 無法讀到、圖示看不到的內容，一律列成 ISSUE。
- 不要補充企劃書沒有的數值、流程、UI 或常識推測。
- 每條 CASE 要可執行、可觀察、可判定。

# 輸出格式
只輸出以下純文字行，不要輸出 Markdown 表格或額外說明：

SYSTEM: <系統名稱>
CASE: <項目>|<類型>|<優先度>|<自動化>|<TC內容>
ISSUE: <待釐清問題>
"""


def parse_output(output: str) -> dict[str, Any]:
    system = ""
    cases: list[tuple[str, str, str, str, str]] = []
    issues: list[str] = []
    for line in str(output or "").splitlines():
        text = line.strip()
        text = re.sub(r"^[-*]\s*", "", text)
        m = re.match(r"^SYSTEM:\s*(.+)$", text)
        if m:
            system = m.group(1).strip()
            continue
        m = re.match(r"^CASE:\s*(.+)$", text)
        if m:
            parts = [p.strip() for p in m.group(1).split("|")]
            if len(parts) >= 5:
                item, typ, priority, automation = parts[:4]
                tc = "|".join(parts[4:]).strip()
                if typ not in VALID_TYPES:
                    typ = "操作確認"
                if priority not in VALID_PRIORITIES:
                    priority = "P1"
                if automation not in VALID_AUTOMATION:
                    automation = "M"
                if item and tc:
                    cases.append((item, typ, priority, automation, tc))
            continue
        m = re.match(r"^ISSUE:\s*(.+)$", text)
        if m:
            issues.append(m.group(1).strip())
    return {"system": system, "cases": cases, "issues": issues}


def write_testcase_xlsx(system: str, cases: list[tuple],
                        issues: list[str], doc_name: str) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:
        raise RuntimeError("缺少 openpyxl，無法寫出 TestCase xlsx") from e

    ensure_dirs()
    out = TESTCASE_DIR / f"{_safe_stem(doc_name)}_TestCase.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = (re.sub(r'[\\/:*?"<>|]+', "_", system or "系統") or "系統")[:31]
    header = ["項目", "類型", "TC內容", "PASS/FAIL", "Bug Key", "備註",
              "PASS", "FAIL", "N/A", "Total", "TC數量"]
    ws.append(header)
    header_font = Font(bold=True)
    header_fill = PatternFill("solid", start_color="D9E1F2")
    for col in range(1, len(header) + 1):
        ws.cell(1, col).font = header_font
        ws.cell(1, col).fill = header_fill
    for col, width in {"A": 18, "B": 12, "C": 86, "D": 11, "E": 12,
                       "F": 24, "G": 7, "H": 7, "I": 7, "J": 7, "K": 9}.items():
        ws.column_dimensions[col].width = width
    wrap = Alignment(vertical="top", wrap_text=True)
    prev_item = None
    for item, typ, _priority, _automation, tc in cases:
        ws.append([item if item != prev_item else "", typ, tc, "", "", ""])
        ws.cell(ws.max_row, 3).alignment = wrap
        prev_item = item
    ws["G2"] = '=COUNTIF(D:D,"PASS")'
    ws["H2"] = '=COUNTIF(D:D,"FAIL")'
    ws["I2"] = '=COUNTIF(D:D,"N/A")'
    ws["J2"] = "=G2+H2+I2"
    ws["K2"] = "=COUNTA(C2:C10000)"

    ws_meta = wb.create_sheet("優先度與自動化")
    ws_meta.append(["#", "項目", "類型", "優先度", "自動化(A=AI白話/S=腳本回歸/M=手動)", "TC內容"])
    for col in range(1, 7):
        ws_meta.cell(1, col).font = header_font
    for col, width in {"A": 5, "B": 18, "C": 12, "D": 8, "E": 34, "F": 86}.items():
        ws_meta.column_dimensions[col].width = width
    for index, (item, typ, priority, automation, tc) in enumerate(cases, 1):
        ws_meta.append([index, item, typ, priority, automation, tc])
        ws_meta.cell(ws_meta.max_row, 6).alignment = wrap

    ws_issue = wb.create_sheet("待釐清問題")
    ws_issue.column_dimensions["A"].width = 120
    ws_issue.append([f"企劃書：{doc_name}"])
    ws_issue.append([f"產出時間：{datetime.now():%Y-%m-%d %H:%M}"])
    ws_issue.append([f"案例數：{len(cases)}"])
    ws_issue.append([])
    ws_issue.append(["待釐清問題"])
    ws_issue.cell(ws_issue.max_row, 1).font = header_font
    for index, issue in enumerate(issues, 1):
        ws_issue.append([f"{index}. {issue}"])
        ws_issue.cell(ws_issue.max_row, 1).alignment = wrap

    try:
        wb.save(out)
    except PermissionError:
        out = TESTCASE_DIR / f"{_safe_stem(doc_name)}_TestCase_{datetime.now():%H%M%S}.xlsx"
        wb.save(out)
    return out


def list_testcases() -> list[dict[str, Any]]:
    if not TESTCASE_DIR.exists():
        return []
    rows = []
    for path in sorted(TESTCASE_DIR.glob("*_TestCase*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        rows.append({
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return rows


def autopush_files(paths: list[Path], message: str) -> str:
    files = [os.path.relpath(p, ROOT) for p in paths if p and p.exists()]
    if not files:
        return "no files"
    try:
        subprocess.run(["git", "add", "--", *files], cwd=ROOT, check=True,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *files],
                              cwd=ROOT)
        if diff.returncode == 0:
            return "no changes"
        commit = subprocess.run(["git", "commit", "-m", message, "--", *files],
                                cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
        if commit.returncode != 0:
            return (commit.stderr or commit.stdout or "git commit failed").strip()
        push = subprocess.run(["git", "push"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
        if push.returncode != 0:
            return f"commit ok, push failed: {(push.stderr or push.stdout).strip()}"
        return "committed and pushed"
    except Exception as e:
        return f"git failed: {e}"


def generate_testcases(doc_path: str, run_ai, on_progress=None,
                       doc_name: str | None = None,
                       autopush: bool = True) -> dict[str, Any]:
    doc = Path(doc_path)
    if not doc.exists():
        return {"ok": False, "error": f"找不到企劃書：{doc_path}"}
    if doc.suffix.lower() not in SUPPORTED_EXTS:
        return {"ok": False, "error": f"不支援的企劃書格式：{doc.suffix}"}
    ensure_dirs()
    if on_progress:
        on_progress("抽取企劃書內容...")
    text, mode = extract_doc_text(doc)
    if not text.strip() or mode == "pdf-unreadable":
        return {"ok": False, "error": text or "企劃書沒有可讀文字"}
    source_name = _safe_name(doc_name or doc.name)
    prompt = build_prompt(source_name, text, mode)
    if on_progress:
        on_progress("Codex 生成 QA TestCase 中...")
    ai_result = run_ai(prompt)
    output = ai_result.get("output", "") if isinstance(ai_result, dict) else ""
    attempts = ai_result.get("attempts", []) if isinstance(ai_result, dict) else []
    log_path = INPUT_DIR / f"{_safe_stem(source_name)}_gen_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path.write_text(output, encoding="utf-8", errors="replace")
    parsed = parse_output(output)
    if not parsed["cases"]:
        tail = "\n".join(output.strip().splitlines()[-8:])
        return {
            "ok": False,
            "error": f"AI 未輸出任何測試案例。\n{tail}",
            "attempts": attempts,
            "log": str(log_path),
        }
    xlsx = write_testcase_xlsx(
        parsed["system"] or doc.stem,
        parsed["cases"],
        parsed["issues"],
        source_name,
    )
    doc_backup = TESTCASE_DIR / source_name
    try:
        if doc.resolve() != doc_backup.resolve():
            shutil.copy2(doc, doc_backup)
    except OSError:
        doc_backup = None
    git = ""
    if autopush:
        push_paths = [xlsx] + ([doc_backup] if doc_backup else [])
        git = autopush_files(
            push_paths,
            f"[Hibari] 新增 QA TestCase {xlsx.name}",
        )
    return {
        "ok": True,
        "xlsx": str(xlsx),
        "xlsx_name": xlsx.name,
        "source_name": source_name,
        "doc_backup": str(doc_backup) if doc_backup else "",
        "cases": len(parsed["cases"]),
        "issues": len(parsed["issues"]),
        "mode": mode,
        "git": git,
        "attempts": attempts,
        "log": str(log_path),
        "message": (
            f"已生成 {len(parsed['cases'])} 條 TestCase"
            + (f"，{len(parsed['issues'])} 個待釐清問題" if parsed["issues"] else "")
            + (f"；git：{git}" if git else "")
        ),
    }
