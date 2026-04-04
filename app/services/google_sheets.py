"""Google Sheets client for audit trail appends.

Uses the synchronous google-api-python-client wrapped in asyncio.run_in_executor
to avoid blocking the event loop.
"""
import asyncio
import json
import os

import structlog

logger = structlog.get_logger()


class GoogleSheetsClient:
    def __init__(self, service_account_json_path: str, spreadsheet_id: str):
        self._spreadsheet_id = spreadsheet_id
        self._service = None

        if not spreadsheet_id:
            logger.warning("google_sheets_spreadsheet_id_not_configured")
            return

        if not os.path.exists(service_account_json_path):
            logger.warning(
                "google_sheets_sa_file_not_found", path=service_account_json_path
            )
            return

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                service_account_json_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        except Exception as exc:
            logger.error("google_sheets_init_failed", error=str(exc))

    def _append_row_sync(self, values: list, sheet_name: str) -> dict:
        if self._service is None:
            raise RuntimeError("Google Sheets service not initialized")
        range_ = f"{sheet_name}!A1"
        body = {"values": [values]}
        result = (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=range_,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute()
        )
        return result

    def _find_row_by_run_id_sync(self, run_id: str, sheet_name: str) -> int | None:
        if self._service is None:
            return None
        range_ = f"{sheet_name}!A:A"
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=range_)
            .execute()
        )
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if row and row[0] == run_id:
                return i + 1  # 1-indexed
        return None

    async def append_row(self, values: list, sheet_name: str = "Audit Log") -> dict:
        """Append a row to the sheet. Runs sync client in thread executor."""
        if self._service is None:
            logger.warning("google_sheets_not_available_skip_append")
            return {}
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._append_row_sync, values, sheet_name
            )
            return result
        except Exception as exc:
            logger.error("google_sheets_append_failed", error=str(exc))
            return {}

    async def find_row_by_run_id(self, run_id: str, sheet_name: str = "Audit Log") -> int | None:
        if self._service is None:
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._find_row_by_run_id_sync, run_id, sheet_name
            )
        except Exception as exc:
            logger.error("google_sheets_find_failed", run_id=run_id, error=str(exc))
            return None
