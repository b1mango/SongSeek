import csv
import io
import math
import re
import zipfile
from datetime import time, timedelta
from pathlib import Path

from openpyxl import load_workbook

from ..errors import SongSeekError
from ..models import ColumnMapping, ParsedRow, ParseResult

ALIASES = {
    "title": {"歌名", "歌曲", "曲名", "title", "name", "song"},
    "artist": {"歌手", "艺人", "演唱", "artist", "singer"},
    "album": {"专辑", "album"},
    "duration": {"时长", "duration", "length"},
}


def parse_duration(value) -> float | None:
    if value is None or str(value).strip() in ("", "-", "—"):
        return None
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1e6
    if isinstance(value, timedelta):
        return value.total_seconds()
    text = str(value).strip()
    try:
        parts = text.split(":")
        if len(parts) in (2, 3):
            nums = [float(part) for part in parts]
            if any(n < 0 for n in nums) or any(n >= 60 for n in nums[1:]):
                raise ValueError
            result = sum(n * 60 ** i for i, n in enumerate(reversed(nums)))
        elif len(parts) == 1:
            result = float(text)
            if result > 10000:
                result /= 1000
        else:
            raise ValueError
        if not math.isfinite(result) or result <= 0:
            raise ValueError
        return result
    except ValueError:
        raise SongSeekError("无法识别时长，请用秒数或 分:秒", "invalid_duration") from None


def identify(columns: list[str], override: dict | None = None) -> ColumnMapping:
    mapping = {field: next((c for c in columns if c.strip().casefold() in aliases), None) for field, aliases in ALIASES.items()}
    if override:
        unknown = set(override) - set(ALIASES) - {"table"}
        if unknown:
            raise SongSeekError("列映射字段不正确", "invalid_mapping")
        mapping.update({k: v for k, v in override.items() if k in ALIASES})
    if any(value is not None and value not in columns for value in mapping.values()):
        raise SongSeekError("列映射引用了不存在的列", "invalid_mapping")
    return ColumnMapping(**mapping)


def table_result(table: list[list], name: str, source: str, mapping: dict | None = None) -> ParseResult:
    if not table:
        raise SongSeekError("表格为空", "empty_import")
    columns = [str(v).strip() if v is not None else "" for v in table[0]]
    if len(set(columns)) != len(columns):
        raise SongSeekError("列名重复，请先修正表头", "invalid_columns")
    selected = identify(columns, mapping)
    if selected.title is None:
        return ParseResult(name=name, source=source, columns=columns, mapping=selected, rows=[], warnings=["未识别出歌名列，请指定列映射后重新解析"])
    rows, warnings = [], []
    indices = {field: columns.index(column) if column is not None else None for field, column in selected.model_dump().items()}
    for number, cells in enumerate(table[1:], 2):
        if not any(v is not None and str(v).strip() for v in cells):
            continue
        values = {field: cells[index] if index is not None and index < len(cells) else None for field, index in indices.items()}
        title = str(values["title"] or "").strip()
        if not title:
            warnings.append(f"第 {number} 行缺少歌名，已忽略")
            continue
        try:
            duration = parse_duration(values["duration"])
        except SongSeekError:
            duration = None
            warnings.append(f"第 {number} 行时长无法识别，已留空")
        rows.append(ParsedRow(title=title, artist=str(values["artist"] or "").strip(), album=str(values["album"] or "").strip(), duration=duration))
    return ParseResult(name=name, source=source, columns=columns, mapping=selected, rows=rows, warnings=warnings)


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SongSeekError("文件编码无法识别，请另存为 UTF-8", "invalid_encoding")


def markdown_tables(text: str) -> list[list[list[str]]]:
    def cells(line: str) -> list[str]:
        return [v.strip().replace(r"\|", "|") for v in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
    tables = []
    lines = text.splitlines()
    index = 0
    while index + 1 < len(lines):
        header, separator = cells(lines[index]), cells(lines[index + 1])
        if "|" in lines[index] and len(header) == len(separator) and all(re.fullmatch(r":?-{3,}:?", c) for c in separator):
            table = [header]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table.append(cells(lines[index]))
                index += 1
            tables.append(table)
        else:
            index += 1
    return tables


def parse_text(text: str, name: str = "粘贴清单", mapping: dict | None = None) -> ParseResult:
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and sum("\t" in line for line in lines) * 2 >= len(lines):
        return parse_tsv(lines, name, mapping)
    rows, dashed = [], 0
    for line in lines:
        line = re.sub(r"^\s*(?:(?:[-*+•]\s+)|(?:\(?\d+[.)、．]\s*)|(?:\d+\s+))+", "", line).strip()
        if not line:
            continue
        parts = re.split(r"\s+[-–—]\s+|\s*/\s*", line, maxsplit=1)
        title, artist = parts if len(parts) == 2 else (line, "")
        dashed += len(parts) == 2 and bool(re.search(r"\s+[-–—]\s+", line))
        rows.append(ParsedRow(title=title.strip(), artist=artist.strip()))
    warnings = [f"有 {dashed} 行按「歌名 - 歌手」解析；如果你的清单是「歌手 - 歌名」，请调换顺序后再粘贴"] if dashed else []
    return ParseResult(name=name, source="text", rows=rows, warnings=warnings)


def parse_tsv(lines: list[str], name: str, mapping: dict | None) -> ParseResult:
    """Tab-separated text, usually copied straight out of a spreadsheet."""
    table = [line.split("\t") for line in lines]
    width = max(len(row) for row in table)
    table = [row + [""] * (width - len(row)) for row in table]
    header = [cell.strip() for cell in table[0]]
    if len(set(header)) == len(header) and identify(header).title is not None:
        result = table_result(table, name, "text", mapping)
    else:
        columns = [f"第{n}列" for n in range(1, width + 1)]
        guess: dict = {"title": columns[0], "artist": columns[1] if width > 1 else None}
        for index, column in enumerate(columns[2:], 2):
            values = [row[index].strip() for row in table if row[index].strip()]
            if values and sum(bool(re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", v)) for v in values) * 2 >= len(values):
                guess["duration"] = column
                break
        result = table_result([columns, *table], name, "text", guess | (mapping or {}))
        result.warnings.insert(0, "表格没有表头，已按「第 1 列歌名、第 2 列歌手」猜测，可在下方调整列对应")
    return result


def parse_file(data: bytes, filename: str, mapping: dict | None = None) -> ParseResult:
    path = Path(filename)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(member.file_size for member in archive.infolist()) > 100 * 1024 * 1024:
                    raise SongSeekError("XLSX 解压后不能超过 100MB", "file_too_large", 413)
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                table = list(workbook.active.values)
            finally:
                workbook.close()
        except SongSeekError:
            raise
        except Exception:
            raise SongSeekError("无法读取 XLSX 文件", "invalid_file") from None
        return table_result(table, path.stem, "xlsx", mapping)
    text = decode(data)
    if suffix == ".csv":
        return table_result(list(csv.reader(io.StringIO(text))), path.stem, "csv", mapping)
    if suffix == ".md":
        tables = markdown_tables(text)
        if mapping and "table" in mapping:
            index = mapping["table"]
            if not isinstance(index, int) or not 0 <= index < len(tables):
                raise SongSeekError("Markdown 表格序号不存在（从 0 开始）", "invalid_mapping")
            table = tables[index]
        else:
            eligible = []
            for candidate in tables:
                try:
                    if identify(candidate[0], mapping).title is not None:
                        eligible.append(candidate)
                except SongSeekError:
                    continue
            if not eligible:
                raise SongSeekError("未找到包含歌名列的 Markdown 表格", "missing_title_column")
            table = max(eligible, key=lambda t: len(t[0]))
        return table_result(table, path.stem, "md", mapping)
    if suffix != ".txt":
        raise SongSeekError("只支持 xlsx、csv、md、txt 文件", "unsupported_file")
    return parse_text(text, path.stem)
