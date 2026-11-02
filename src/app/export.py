"""목록 엑셀 내보내기 공통. openpyxl write-only 모드로 메모리 사용을 최소화한다."""

import io
from collections.abc import Callable, Iterable
from datetime import datetime
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from openpyxl import Workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

Column = tuple[str, Callable]  # (머리글, 행 → 값)


def xlsx_response(filename: str, columns: list[Column], rows: Iterable) -> StreamingResponse:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet()
    ws.append([title for title, _ in columns])
    for row in rows:
        ws.append([_cell(get(row)) for _, get in columns])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    stamp = datetime.now().strftime("%Y%m%d")
    name = quote(f"{filename}_{stamp}.xlsx")
    return StreamingResponse(
        buf,
        media_type=XLSX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


def _cell(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if isinstance(value, dict | list):
        return str(value)
    return value
