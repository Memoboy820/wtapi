from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import struct

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
import httpx

from src.constants import (
    API_URL, MAX_SESSION_ID, HEADER_SIZE, SIGNATURE, logger,
    RETRY_STATUSES, PARAMS, BASE_URL, STATE_FILE, API_RESPONSE_FILE,
    BATTLE_LIMIT, DOWNLOAD_CONCURRENCY, REPLAY_FOLDER, REQUEST_DELAY,
    PROJECT_ROOT,
)
from src.services.wrpl_parser import parse_wrpl_folder


load_dotenv(PROJECT_ROOT / ".env")

# макс одновременно скачиваемых частей 
PART_DOWNLOAD_CONCURRENCY = 3


class ReplayAPIError(RuntimeError):
    pass


class ReplayDownloadError(RuntimeError):
    pass


def normalize_session_id(session_id: str | int, *, id_format: str = "auto") -> str:
    if id_format not in {"auto", "decimal", "hex"}:
        raise ReplayDownloadError("id_format должен быть auto, decimal или hex")

    if isinstance(session_id, bool):
        raise ReplayDownloadError("Некорректный session_id")

    if isinstance(session_id, int):
        if session_id < 0 or session_id > MAX_SESSION_ID:
            raise ReplayDownloadError("session_id выходит за границы unsigned 64-bit")

        return format(session_id, "016x")

    if not isinstance(session_id, str):
        raise ReplayDownloadError("session_id должен быть строкой или целым числом")

    value = session_id.strip().lower()

    if not value:
        raise ReplayDownloadError("Пустой session_id")

    if id_format == "auto":
        if value.startswith("0x"):
            id_format = "hex"
        elif re.fullmatch(r"[0-9]+", value) and len(value) > 16:
            id_format = "decimal"
        else:
            id_format = "hex"

    if id_format == "decimal":
        if not re.fullmatch(r"[0-9]+", value):
            raise ReplayDownloadError(f"Ожидался десятичный session_id: {value!r}")

        number = int(value, 10)

    else:
        value = value.removeprefix("0x")

        if not re.fullmatch(r"[0-9a-f]{1,16}", value):
            raise ReplayDownloadError(f"Некорректный hex session_id: {value!r}")

        number = int(value, 16)

    if number > MAX_SESSION_ID:
        raise ReplayDownloadError("session_id выходит за границы unsigned 64-bit")

    return format(number, "016x")


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")

    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    finally:
        temporary.unlink(missing_ok=True)


def _save_json(path: Path, data: object) -> None:
    save_json(path, data)


def _check_header(header: bytes, file_size: int) -> bool:
    if len(header) < max(HEADER_SIZE, 688):
        raise ReplayDownloadError("Слишком короткий файл WRPL")

    signature = struct.unpack_from("<I", header, 0)[0]

    if signature != SIGNATURE:
        raise ReplayDownloadError("Неверная сигнатура WRPL")

    footer_offset = struct.unpack_from("<I", header, 684)[0]

    if footer_offset:
        if not HEADER_SIZE <= footer_offset < file_size:
            raise ReplayDownloadError("Файл WRPL содержит некорректное смещение footer")

        return True

    return False


def _inspect_data(data: bytes) -> bool:
    return _check_header(data[:max(HEADER_SIZE, 688)], len(data))


def _inspect_file(path: Path) -> bool:
    with path.open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        header = file.read(max(HEADER_SIZE, 688))

    return _check_header(header, size)


def _save_file(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")

    try:
        temporary.write_bytes(data)
        temporary.replace(path)

    finally:
        temporary.unlink(missing_ok=True)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")

    if not value:
        return None

    try:
        delay = float(value)

        if math.isfinite(delay):
            return max(0.0, delay)

        return None

    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)

        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    except (TypeError, ValueError, OverflowError):
        return None


def _backoff(attempt: int) -> float:
    return min(15.0 * 2**attempt, 300.0) + random.uniform(0, 3)


class ReplayDownloadClient(httpx.AsyncClient):
    def __init__(self, *, concurrency: int, request_delay: float, **kwargs):
        if concurrency < 1:
            raise ReplayDownloadError("concurrency должен быть не меньше 1")

        if not math.isfinite(request_delay) or request_delay < 0:
            raise ReplayDownloadError("request_delay должен быть неотрицательным числом")

        super().__init__(**kwargs)

        self._slots = asyncio.Semaphore(concurrency)
        self._start_lock = asyncio.Lock()
        self._request_delay = request_delay
        self._next_start = 0.0
        self._paused_until = 0.0

    async def _wait_for_start(self) -> None:
        loop = asyncio.get_running_loop()

        async with self._start_lock:
            while True:
                remaining = max(self._next_start, self._paused_until) - loop.time()

                if remaining <= 0:
                    self._next_start = loop.time() + self._request_delay
                    return

                await asyncio.sleep(remaining)

    async def request(self, method: str, url, **kwargs) -> httpx.Response:
        async with self._slots:
            await self._wait_for_start()

            response = await super().request(method, url, **kwargs)

            if response.status_code == 429:
                delay = _retry_after(response)

                if delay is None:
                    delay = _backoff(0)

                delay += random.uniform(0, 2)

                loop = asyncio.get_running_loop()
                self._paused_until = max(self._paused_until, loop.time() + delay)

                logger.warning("HTTP 429: общая пауза новых загрузок на %.1f с", delay)

            return response


async def _request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int,
    error_type: type[RuntimeError],
    **kwargs,
) -> httpx.Response:
    if max_attempts < 1:
        raise error_type("max_attempts должен быть не меньше 1")

    for attempt in range(max_attempts):
        try:
            response = await client.request(method, url, **kwargs)

        except httpx.TransportError as error:
            if attempt == max_attempts - 1:
                raise error_type(f"Ошибка соединения с {url}: {error}") from error

            delay = _backoff(attempt)
            logger.warning("Ошибка соединения. Повтор через %.1f с: %s", delay, url)

            await asyncio.sleep(delay)
            continue

        if response.status_code not in RETRY_STATUSES and response.status_code != 429:
            return response

        if attempt == max_attempts - 1:
            status = response.status_code
            await response.aclose()

            raise error_type(
                f"Сервер возвращает HTTP {status}: {url}. "
                f"Исчерпаны попытки: {max_attempts}"
            )

        delay = _retry_after(response)

        if delay is None:
            delay = _backoff(attempt)
        else:
            delay += random.uniform(0, 2)

        logger.warning("HTTP %s. Повтор через %.1f с: %s", response.status_code, delay, url)

        await response.aclose()
        await asyncio.sleep(delay)

    raise error_type(f"Не удалось получить ответ от {url}")


async def fetch_replays(
    *,
    wrapped_params: bool = False,
    params: dict | None = None,
    max_attempts: int = 6,
    timeout: float = 60.0,
) -> dict | list:
    if max_attempts < 1:
        raise ReplayAPIError("max_attempts должен быть не меньше 1")

    if not math.isfinite(timeout) or timeout <= 0:
        raise ReplayAPIError("timeout должен быть положительным числом")

    cookie = os.getenv("WT_COOKIE", "").strip()

    if not cookie:
        raise ReplayAPIError(f"Не задана WT_COOKIE. Проверь .env в {PROJECT_ROOT}")

    if "\n" in cookie or "\r" in cookie:
        raise ReplayAPIError("WT_COOKIE должна содержать cookies одной строкой")

    request_params = PARAMS.copy()

    if params is not None:
        request_params.update(params)

    payload = {"params": request_params} if wrapped_params else request_params

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False) as client:
        response = await _request_with_retries(
            client, "POST", API_URL,
            max_attempts=max_attempts,
            error_type=ReplayAPIError,
            json=payload,
            headers={
                "Accept": "application/json",
                "Cookie": cookie,
                "Origin": "https://warthunder.com",
                "Referer": "https://warthunder.com/ru/tournament/replay/",
            },
        )

        try:
            if response.status_code != 200:
                content_type = response.headers.get("content-type", "не указан")

                raise ReplayAPIError(
                    f"API вернул HTTP {response.status_code}. "
                    f"Content-Type: {content_type}"
                )

            try:
                result = response.json()

            except ValueError as error:
                raise ReplayAPIError(
                    "API вернул не JSON. Проверь cookies и формат запроса."
                ) from error

            if not isinstance(result, (dict, list)):
                raise ReplayAPIError(f"Неожиданный формат ответа API: {type(result).__name__}")

            return result

        finally:
            await response.aclose()


async def save_replay_response(
    output_file: str | Path,
    *,
    wrapped_params: bool = False,
    params: dict | None = None,
    max_attempts: int = 6,
    timeout: float = 60.0,
) -> Path:
    result = await fetch_replays(
        wrapped_params=wrapped_params,
        params=params,
        max_attempts=max_attempts,
        timeout=timeout,
    )

    output = Path(output_file).expanduser().resolve()

    try:
        await asyncio.to_thread(save_json, output, result)

    except OSError as error:
        raise ReplayAPIError(f"Не удалось сохранить ответ API: {error}") from error

    return output


async def download_replay(
    session_id: str | int,
    save_to: str | Path,
    *,
    parts_count: int,
    client: ReplayDownloadClient | None = None,
    id_format: str = "auto",
    request_delay: float = REQUEST_DELAY,
    max_attempts: int = 6,
    max_parts: int = 1000,
    timeout: float = 90.0,
) -> Path:
    original_id = str(session_id)
    session_id = normalize_session_id(session_id, id_format=id_format)

    if original_id != session_id:
        logger.info("ID боя преобразован: %s → %s", original_id, session_id)

    if isinstance(parts_count, bool) or not isinstance(parts_count, int):
        raise ReplayDownloadError("parts_count должен быть целым числом")

    if not 1 <= parts_count <= max_parts:
        raise ReplayDownloadError(f"Недопустимое количество частей: {parts_count}")

    if max_attempts < 1:
        raise ReplayDownloadError("max_attempts должен быть не меньше 1")

    if not math.isfinite(request_delay) or request_delay < 0:
        raise ReplayDownloadError("request_delay должен быть неотрицательным числом")

    if not math.isfinite(timeout) or timeout <= 0:
        raise ReplayDownloadError("timeout должен быть положительным числом")


    if client is None:
        async with ReplayDownloadClient(
            concurrency=PART_DOWNLOAD_CONCURRENCY,
            request_delay=request_delay,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=PART_DOWNLOAD_CONCURRENCY,
                max_keepalive_connections=PART_DOWNLOAD_CONCURRENCY,
            ),
        ) as own_client:
            return await download_replay(
                session_id=session_id,
                save_to=save_to,
                parts_count=parts_count,
                client=own_client,
                id_format="hex",
                request_delay=request_delay,
                max_attempts=max_attempts,
                max_parts=max_parts,
                timeout=timeout,
            )

    folder = Path(save_to).expanduser().resolve() / session_id

    try:
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)

    except OSError as error:
        raise ReplayDownloadError(f"Не удалось создать папку {folder}: {error}") from error

    async def download_part(index: int) -> bool:
        filename = f"{index:04d}.wrpl"
        path = folder / filename

        try:
            if await asyncio.to_thread(path.is_file):
                try:
                    file_has_footer = await asyncio.to_thread(_inspect_file, path)

                except (OSError, ReplayDownloadError):
                    logger.warning("Бой %s: %s повреждён, скачиваю заново", session_id, filename)

                else:
                    logger.info("Бой %s: используется сохранённый %s", session_id, filename)
                    return file_has_footer

            url = f"{BASE_URL}/{session_id}/{filename}"

            response = await _request_with_retries(
                client, "GET", url,
                max_attempts=max_attempts,
                error_type=ReplayDownloadError,
            )

            try:
                if response.status_code == 404:
                    raise ReplayDownloadError(
                        f"Бой {session_id}: отсутствует ожидаемая часть {filename}. "
                        f"API указал частей: {parts_count}"
                    )

                response.raise_for_status()
                data = response.content

                file_has_footer = await asyncio.to_thread(_inspect_data, data)
                await asyncio.to_thread(_save_file, path, data)

                logger.info(
                    "Бой %s: скачан %s, %.2f МБ",
                    session_id, filename, len(data) / 1024 / 1024,
                )

                return file_has_footer

            finally:
                await response.aclose()

        except ReplayDownloadError:
            raise

        except (OSError, httpx.HTTPError) as error:
            raise ReplayDownloadError(
                f"Бой {session_id}: ошибка загрузки {filename}: {error}"
            ) from error

    tasks = [
        asyncio.create_task(download_part(index))
        for index in range(parts_count)
    ]

    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    errors = []

    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome

            errors.append(f"{index:04d}.wrpl: {type(outcome).__name__}: {outcome}")

    if errors:
        raise ReplayDownloadError(
            f"Бой {session_id}: не удалось загрузить все части:\n"
            + "\n".join(errors[:5])
        )

    if not any(outcome is True for outcome in outcomes):
        raise ReplayDownloadError(
            f"Бой {session_id}: все ожидаемые части получены, но footer не найден"
        )

    logger.info("Бой %s скачан полностью. Частей: %s. Папка: %s", session_id, parts_count, folder)

    return folder


def load_state() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    except FileNotFoundError:
        return {"battles": {}}

    if (
        not isinstance(value, dict)
        or not isinstance(value.get("battles"), dict)
        or not all(isinstance(entry, dict) for entry in value["battles"].values())
    ):
        raise ValueError(f"Некорректный файл состояния: {STATE_FILE}")

    return value


async def save_state(state: dict) -> None:
    await asyncio.to_thread(save_json, STATE_FILE, state)


def extract_session_ids(response: dict | list) -> list[str]:
    battles: dict[str, int | float] = {}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if "sessionIdHex" in value:
                session_id = value.get("sessionIdHex")

                if not isinstance(session_id, str):
                    return

                session_id = session_id.strip().lower()

                if not re.fullmatch(r"[0-9a-f]{16}", session_id):
                    return

                if value.get("clanBattle") is not True:
                    return

                if value.get("gameType") != "clanBattle":
                    return

                if value.get("isCompleted") is not True:
                    return

                start_time = value.get("startTime", 0)

                if not isinstance(start_time, (int, float)):
                    start_time = 0

                previous = battles.get(session_id)

                if previous is None or start_time > previous:
                    battles[session_id] = start_time

                return

            for item in value.values():
                walk(item)

        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(response)

    if not battles:
        raise ValueError(
            "API не вернул подходящих завершённых клановых боёв с sessionIdHex. "
            f"Проверь ответ: {API_RESPONSE_FILE}"
        )

    return sorted(
        battles,
        key=lambda session_id: battles[session_id],
        reverse=True,
    )[:BATTLE_LIMIT]


def extract_parts_counts(response: dict | list) -> dict[str, int]:
    counts: dict[str, int] = {}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            session_id = value.get("sessionIdHex")
            parts_count = value.get("partsCount")

            if isinstance(session_id, str):
                session_id = session_id.strip().lower()

                if (
                    re.fullmatch(r"[0-9a-f]{16}", session_id)
                    and isinstance(parts_count, int)
                    and not isinstance(parts_count, bool)
                    and parts_count > 0
                ):
                    counts[session_id] = parts_count

            for item in value.values():
                walk(item)

        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(response)

    return counts


async def download_battles(
    session_ids: list[str],
    parts_counts: dict[str, int],
) -> list[Path | Exception]:
    if DOWNLOAD_CONCURRENCY < 1:
        raise ReplayDownloadError("DOWNLOAD_CONCURRENCY должен быть не меньше 1")

    battle_slots = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async with ReplayDownloadClient(
        concurrency=PART_DOWNLOAD_CONCURRENCY,
        request_delay=REQUEST_DELAY,
        timeout=httpx.Timeout(90.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=PART_DOWNLOAD_CONCURRENCY,
            max_keepalive_connections=PART_DOWNLOAD_CONCURRENCY,
        ),
    ) as client:

        async def download_one(session_id: str) -> Path:
            async with battle_slots:
                parts_count = parts_counts.get(session_id)

                if parts_count is None:
                    raise ReplayDownloadError(
                        f"API не указал корректный partsCount для боя {session_id}"
                    )

                logger.info("Начинаю скачивание боя %s, частей=%s", session_id, parts_count)

                try:
                    folder = await download_replay(
                        session_id=session_id,
                        save_to=REPLAY_FOLDER,
                        parts_count=parts_count,
                        client=client,
                        id_format="hex",
                    )

                except Exception:
                    logger.exception("Ошибка скачивания боя %s", session_id)
                    raise

                logger.info("Бой %s скачан: %s", session_id, folder)

                return folder

        tasks = [
            asyncio.create_task(download_one(session_id))
            for session_id in session_ids
        ]

        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

    results: list[Path | Exception] = []

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome

            results.append(outcome)
        else:
            results.append(outcome)

    return results


async def process_battle(session_id: str, folder: Path, state: dict) -> None:
    entry = state["battles"][session_id]

    entry["status"] = "processing"
    entry["folder"] = str(folder)
    entry.pop("error", None)

    await save_state(state)

    try:
        logger.info("Разбираю бой %s", session_id)

        result = await asyncio.to_thread(parse_wrpl_folder, folder)
        result_file = folder / "result.json"

        await asyncio.to_thread(save_json, result_file, result)

    except Exception as error:
        entry["status"] = "error"
        entry["error_stage"] = "parsing"
        entry["error"] = f"{type(error).__name__}: {error}"

        logger.exception("Ошибка разбора боя %s", session_id)

    else:
        entry["status"] = "done"
        entry["result_file"] = str(result_file)
        entry["result"] = result

        entry.pop("error", None)
        entry.pop("error_stage", None)

        logger.info("Бой %s обработан. Результат: %s", session_id, result_file)

    await save_state(state)


async def check_replays(state: dict) -> None:
    response = await fetch_replays(wrapped_params=False)

    await asyncio.to_thread(save_json, API_RESPONSE_FILE, response)

    session_ids = extract_session_ids(response)
    parts_counts = extract_parts_counts(response)

    logger.info("ID из текущего ответа API: %s", ", ".join(session_ids))

    for session_id in session_ids:
        state["battles"].setdefault(
            session_id,
            {"status": "pending", "attempts": 0},
        )

    pending = [
        session_id
        for session_id in session_ids
        if state["battles"][session_id].get("status") != "done"
    ]

    logger.info("Получено ID: %s. В очереди: %s", len(session_ids), len(pending))

    if not pending:
        await save_state(state)
        logger.info("Новых боёв для обработки нет")
        return

    for session_id in pending:
        entry = state["battles"][session_id]
        attempts = entry.get("attempts", 0)

        if not isinstance(attempts, int):
            attempts = 0

        entry["attempts"] = attempts + 1
        entry["status"] = "downloading"

        entry.pop("error", None)
        entry.pop("error_stage", None)
        entry.pop("result", None)
        entry.pop("result_file", None)

    await save_state(state)

    logger.info(
        "Запускаю скачивание %s боёв. Одновременно боёв: до %s. "
        "Активных запросов частей суммарно: до %s. Интервал стартов: %.2f с",
        len(pending), DOWNLOAD_CONCURRENCY,
        PART_DOWNLOAD_CONCURRENCY, REQUEST_DELAY,
    )

    download_results = await download_battles(pending, parts_counts)
    downloaded: list[tuple[str, Path]] = []

    for session_id, outcome in zip(pending, download_results, strict=True):
        entry = state["battles"][session_id]

        if isinstance(outcome, Exception):
            entry["status"] = "error"
            entry["error_stage"] = "downloading"
            entry["error"] = f"{type(outcome).__name__}: {outcome}"

        else:
            entry["status"] = "downloaded"
            entry["folder"] = str(outcome)

            downloaded.append((session_id, outcome))

    await save_state(state)

    logger.info(
        "Скачивание завершено. Успешно: %s. Ошибок: %s",
        len(downloaded), len(pending) - len(downloaded),
    )

    for session_id, folder in downloaded:
        await process_battle(session_id=session_id, folder=folder, state=state)

    done_count = sum(
        state["battles"][session_id].get("status") == "done"
        for session_id in pending
    )

    logger.info(
        "Обработка завершена. Успешно: %s. Ошибок: %s",
        done_count, len(pending) - done_count,
    )