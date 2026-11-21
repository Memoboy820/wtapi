from __future__ import annotations

import importlib
import json
import logging
import re
import struct

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

import lz4.block
import zstandard

from src.constants import MAX_OUTPUT, HEADER_SIZE, SIGNATURE, FIELD_SIZES, MODEL_PATTERN, SLOT_PREFIX


logger = logging.getLogger(__name__)


class ReplayServiceError(ValueError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayServiceError(message)


class BitReader:
    def __init__(self, data: bytes, position: int = 0):
        self.data = data
        self.position = position

    def read(self, count: int) -> int:
        check(
            count >= 0
            and self.position >= 0
            and self.position + count <= len(self.data) * 8,
            "Выход за границы битового блока",
        )

        value = 0

        for _ in range(count):
            byte = self.data[self.position // 8]
            shift = 7 - self.position % 8

            value = (value << 1) | ((byte >> shift) & 1)
            self.position += 1

        return value

    def varint(self) -> int:
        value = 0

        for shift in range(0, 70, 7):
            byte = self.read(8)
            value |= (byte & 127) << shift

            if byte < 128:
                return value

        raise ReplayServiceError("Слишком длинный varint")


def read_varint(data: bytes, position: int) -> tuple[int, int]:
    check(0 <= position <= len(data), "Некорректная позиция varint")

    reader = BitReader(data, position * 8)
    value = reader.varint()

    return value, reader.position // 8


def field_bytes(data: bytes, position: int, bit_count: int) -> bytes:
    check(
        bit_count >= 0 and bit_count % 8 == 0,
        "Ожидалось поле с целым количеством байтов",
    )

    reader = BitReader(data, position)

    return bytes(reader.read(8) for _ in range(bit_count // 8))


def unpack_zstd(data: bytes) -> bytes:
    check(bool(data), "Пустой Zstandard-блок")

    result = zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_OUTPUT)

    check(len(result) <= MAX_OUTPUT, "Распакованный Zstandard-блок слишком большой")

    return result


def unpack_lz4(data: bytes) -> bytes:
    check(bool(data), "Пустой LZ4-блок")

    result = lz4.block.decompress(data, uncompressed_size=MAX_OUTPUT)

    check(len(result) <= MAX_OUTPUT, "Распакованный LZ4-блок слишком большой")

    return result


def read_header(data: bytes) -> dict:
    check(len(data) >= max(HEADER_SIZE, 750), "Слишком короткий файл WRPL")
    check(struct.unpack_from("<I", data, 0)[0] == SIGNATURE, "Неверная сигнатура WRPL")

    return {
        "session_id": str(struct.unpack_from("<Q", data, 732)[0]),
        "footer_offset": struct.unpack_from("<I", data, 684)[0],
        "settings_size": struct.unpack_from("<H", data, 748)[0],
    }


def replay_part_sort_key(path: Path) -> tuple:
    chunks = re.split(r"(\d+)", path.name.casefold())

    natural = tuple(
        (1, int(chunk)) if chunk.isdigit() else (0, chunk)
        for chunk in chunks
        if chunk
    )

    return natural, str(path)


def select_replay_files(
    files: Iterable[str | Path] | str | Path,
    *,
    session_id: str | None = None,
) -> list[Path]:
    if isinstance(files, (str, Path)):
        files = [files]

    paths: list[Path] = []
    seen: set[Path] = set()

    for file in files:
        path = Path(file).expanduser().resolve()

        check(path.is_file(), f"Не найден файл: {path}")
        check(path.suffix.lower() == ".wrpl", f"Ожидался файл .wrpl: {path.name}")

        if path not in seen:
            seen.add(path)
            paths.append(path)

    check(bool(paths), "Не передано ни одного файла .wrpl")

    groups: dict[str, list[Path]] = defaultdict(list)

    for path in paths:
        try:
            with path.open("rb") as file:
                header_data = file.read(max(HEADER_SIZE, 750))

            header = read_header(header_data)

        except Exception as error:
            raise ReplayServiceError(
                f"Не удалось прочитать заголовок {path.name}: {error}"
            ) from error

        groups[header["session_id"]].append(path)

    if session_id is None:
        check(
            len(groups) == 1,
            "Переданы файлы нескольких боёв. Укажи session_id. Найдены: "
            + ", ".join(sorted(groups)),
        )

        session_id = next(iter(groups))

    else:
        session_id = str(session_id)

    check(session_id in groups, f"Среди переданных файлов не найден бой {session_id}")

    return sorted(groups[session_id], key=replay_part_sort_key)


def packet_size(data: bytes, position: int) -> tuple[int, int]:
    check(0 <= position < len(data), "Отсутствует размер пакета")

    first = data[position]
    position += 1

    if first & 128:
        return first & 127, position

    for flag, count, mask in (
        (64, 1, 0x4000),
        (32, 2, 0x200000),
        (16, 3, 0x10000000),
    ):
        if first & flag:
            check(position + count <= len(data), "Обрезан размер пакета")

            value = int.from_bytes(
                bytes([first]) + data[position:position + count],
                "big",
            )

            return value ^ mask, position + count

    check(position + 4 <= len(data), "Обрезан длинный размер пакета")

    return struct.unpack_from("<I", data, position)[0], position + 4


def packets(data: bytes):
    position = 0
    current_time = 0

    while position < len(data):
        offset = position
        length, position = packet_size(data, position)

        if length == 0:
            continue

        end = position + length

        check(length >= 2 and end <= len(data), f"Некорректный пакет: {offset}")

        kind = struct.unpack_from("<H", data, position)[0]
        position += 2

        if not kind & 16:
            check(position + 4 <= end, "Отсутствует время пакета")

            current_time = struct.unpack_from("<I", data, position)[0]
            position += 4

        yield offset, current_time, kind & ~16, data[position:end]

        position = end


def slot_messages(payload: bytes):
    check(len(payload) >= 5, "Короткий MPI-пакет слотов")

    position = 5

    if payload[4]:
        _, position = read_varint(payload, position)
        _, position = read_varint(payload, position)

        payload = unpack_zstd(payload[position:])
        position = 0

    check(position + 2 <= len(payload), "Нет количества MPI-объектов")

    count = struct.unpack_from("<H", payload, position)[0]
    position += 2

    for _ in range(count):
        check(position + 2 <= len(payload), "Нет размера MPI-объекта")

        length = struct.unpack_from("<H", payload, position)[0]
        position += 2
        end = position + length

        check(length >= 2 and end <= len(payload), "Некорректный MPI-объект")

        object_id = struct.unpack_from("<H", payload, position)[0]
        position += 2

        if object_id & 2047 == 2047 and object_id != 65535:
            _, position = read_varint(payload[:end], position)

        check(position <= end, "Заголовок вышел за границу MPI-объекта")

        if object_id >> 11 == 14:
            yield object_id & 2047, payload[position:end]

        position = end

    check(position == len(payload), "Лишние байты в конце MPI-пакета")


def slot_fields(data: bytes) -> list[tuple[int, int, int]]:
    check(len(data) >= 4, "Короткий заголовок полей слота")

    table_offset, info = struct.unpack_from("<HH", data, 0)

    count = info & 4095
    index_bits = info >> 12

    check(4 <= table_offset <= len(data), "Некорректное смещение таблицы полей")
    check(0 < index_bits <= 12 or count == 0, "Некорректная ширина индекса поля")

    table = BitReader(data, table_offset * 8)
    sizes = []

    for _ in range(count):
        selector = table.read(3)

        if selector == 0:
            size = table.varint()
        else:
            size = FIELD_SIZES[selector]

        check(isinstance(size, int) and size >= 0, "Некорректный размер поля слота")
        sizes.append(size)

    check(
        32 + sum(sizes) <= table_offset * 8,
        "Значения полей выходят за таблицу индексов",
    )

    values = BitReader(data, 32)
    indices = BitReader(data, ((32 + sum(sizes) + 7) // 8) * 8)

    result = []
    seen = set()

    for bit_count in sizes:
        start = values.position
        values.position += bit_count

        index = indices.read(index_bits)

        check(index not in seen, "Повторяющийся индекс поля")

        seen.add(index)
        result.append((index, bit_count, start))

    check(
        (indices.position + 7) // 8 == table_offset,
        "Не сходятся границы индексов полей",
    )
    check(
        (table.position + 7) // 8 == len(data),
        "Лишние байты после таблицы полей",
    )

    return result


def construction_records(
    payload: bytes,
    templates: dict[int, str],
    components: set[int],
) -> list[tuple[str, str, bytes]]:
    check(bool(payload), "Пустое сообщение ECS")

    data = unpack_lz4(payload[1:]) if payload[0] == 37 else payload[1:]

    check(bool(data), "Пустое содержимое сообщения ECS")

    position = 1
    result = []

    for _ in range(data[0] + 1):
        check(position < len(data), "Отсутствует ECS-идентификатор")

        width = 5 - (data[position] & 3)

        check(position + width <= len(data), "Обрезан ECS-идентификатор")

        reference = data[position:position + width].hex()
        position += width

        length, position = read_varint(data, position)

        check(position + length <= len(data), "Обрезан блок ECS")

        block = data[position:position + length]
        position += length

        template_id, bp = read_varint(block, 0)

        if template_id not in templates:
            name_size, bp = read_varint(block, bp)

            check(
                bp + name_size + 2 <= len(block),
                "Обрезано определение шаблона",
            )

            name = block[bp:bp + name_size].decode("utf-8")
            bp += name_size

            component_count = struct.unpack_from("<H", block, bp)[0]
            bp += 2

            for _ in range(component_count):
                component_id, bp = read_varint(block, bp)

                if component_id not in components:
                    check(bp + 8 <= len(block), "Обрезано определение компонента")

                    components.add(component_id)
                    bp += 8

            templates[template_id] = name

        result.append((reference, templates[template_id], block[bp:]))

    check(position == len(data), "Лишние байты после ECS-записей")

    return result


def extract_model(template: str, payload: bytes) -> str | None:
    if template not in ("aircraft+player_unit", "tank+player_unit"):
        return None

    for match in MODEL_PATTERN.finditer(payload):
        model = match.group(1)
        label = match.group(3)
        length_marker = match.group(2)
        start = match.start(1)

        if start == 0:
            continue

        if payload[start - 1] != len(model):
            continue

        if not length_marker or length_marker[0] != len(label):
            continue

        try:
            name = model.decode("ascii")

        except UnicodeDecodeError:
            continue

        name = name.removeprefix("tankModels/")

        if not name or name == "dummy_plane":
            continue

        if b"_support_" in label:
            continue

        return name

    logger.debug(
        "Не извлечена модель из шаблона %r, размер=%s",
        template, len(payload),
    )

    return None


def footer_decoder() -> Callable:
    try:
        module = importlib.import_module("wt_blk_pybindings")

    except ImportError as error:
        raise ReplayServiceError(
            "Не установлена библиотека wt-blk-pybindings. "
            "Выполни: python -m pip install wt-blk-pybindings"
        ) from error

    decoder = getattr(module, "binary_blk_to_json", None)

    if not callable(decoder):
        namespace = getattr(module, "wt_blk_pybindings", None)
        decoder = getattr(namespace, "binary_blk_to_json", None)

    check(callable(decoder), "В библиотеке нет binary_blk_to_json")

    return decoder


def decode_footer(data: bytes, decoder: Callable) -> dict:
    result = decoder(data)

    if isinstance(result, bytes):
        result = result.decode("utf-8")

    if isinstance(result, str):
        result = json.loads(result)

    check(isinstance(result, dict), "Footer должен быть JSON-объектом")

    return result


def array_values(value: object) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if not isinstance(value, dict):
        return []

    indexed = []

    for key, item in value.items():
        match = re.fullmatch(r"array(\d+)", str(key))

        if match and isinstance(item, dict):
            indexed.append((int(match.group(1)), item))

    indexed.sort(key=lambda pair: pair[0])

    return [item for _, item in indexed]


def get_footer_players(footer: dict) -> list[dict]:
    value = footer.get("player", [])

    if isinstance(value, dict) and "userId" in value:
        return [value]

    return array_values(value)


def pick_footer(footers: list[tuple[str, dict]]) -> dict:
    check(bool(footers), "Нет доступных footer")

    def key(item):
        part, footer = item
        time_played = footer.get("timePlayed", -1)

        if not isinstance(time_played, (int, float)):
            time_played = -1

        return (
            footer.get("status") == "success",
            time_played,
            replay_part_sort_key(Path(part)),
        )

    return max(footers, key=key)[1]


def normalize_vehicle_ids(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []

    result = []
    seen = set()

    for value in values:
        if not isinstance(value, str):
            continue

        vehicle = value.strip().removeprefix("tankModels/")

        if not vehicle or vehicle == "dummy_plane":
            continue

        if vehicle in seen:
            continue

        seen.add(vehicle)
        result.append(vehicle)

    return result


def find_identity(history: list[dict], event: dict) -> dict | None:
    previous = [
        identity
        for identity in history
        if identity["order"] <= event["order"]
        and identity["time_ms"] <= event["time_ms"]
    ]

    if previous:
        return max(previous, key=lambda item: item["order"])

    same_packet = [
        identity
        for identity in history
        if identity["order"] == event["order"]
    ]

    if same_packet:
        return same_packet[-1]

    return None


def find_entity(history: list[dict], event: dict) -> dict | None:
    candidates = [
        entity
        for entity in history
        if entity["time_ms"] <= event["time_ms"]
    ]

    previous = [
        entity
        for entity in candidates
        if entity["order"] <= event["order"]
    ]

    if previous:
        return max(previous, key=lambda item: item["order"])

    same_time = [
        entity
        for entity in candidates
        if entity["time_ms"] == event["time_ms"]
    ]

    if same_time:
        return min(same_time, key=lambda item: item["order"])

    return None


def parse_replay_parts(files: list[Path], *, strict: bool) -> list[dict]:
    decoder = footer_decoder()

    templates: dict[int, str] = {}
    components: set[int] = set()

    identity_history: dict[int, list[dict]] = defaultdict(list)
    entities: dict[str, list[dict]] = defaultdict(list)

    references: list[dict] = []
    footers: list[tuple[str, dict]] = []
    errors: list[str] = []

    packet_order = 0

    def add_error(part: str, offset: int, stage: str, error: Exception) -> None:
        message = f"{part}, смещение {offset}, {stage}: {error}"
        errors.append(message)

        logger.warning("Ошибка разбора: %s", message)

    for path in files:
        logger.debug("Разбор части реплея: %s", path)

        try:
            raw = path.read_bytes()
            header = read_header(raw)

            start = HEADER_SIZE + header["settings_size"]
            end = header["footer_offset"] or len(raw)

            check(start < end <= len(raw), "Некорректные границы потока")

            stream = unpack_zstd(raw[start:end])

        except Exception as error:
            raise ReplayServiceError(
                f"Не удалось прочитать поток {path.name}: {error}"
            ) from error

        try:
            for offset, time_ms, kind, payload in packets(stream):
                packet_order += 1

                if kind == 4 and payload.startswith(SLOT_PREFIX):
                    try:
                        for slot, data in slot_messages(payload):
                            fields = slot_fields(data)
                            fields.sort(key=lambda item: (item[0] != 2, item[0]))

                            for index, bits, position in fields:
                                if index not in (2, 34):
                                    continue

                                value = field_bytes(data, position, bits)

                                if index == 2:
                                    check(len(value) >= 8, "Обрезаны данные игрока")

                                    user_id = str(struct.unpack_from("<Q", value, 0)[0])
                                    name = value[8:].split(b"\x00", 1)[0].decode("utf-8")

                                    identity_history[slot].append({
                                        "id": user_id,
                                        "name": name,
                                        "time_ms": time_ms,
                                        "order": packet_order,
                                    })

                                else:
                                    references.append({
                                        "slot": slot,
                                        "time_ms": time_ms,
                                        "order": packet_order,
                                        "reference": value.hex(),
                                    })

                    except Exception as error:
                        add_error(path.name, offset, "slot", error)

                elif kind == 6 and payload and payload[0] in (36, 37):
                    try:
                        records = construction_records(payload, templates, components)

                        for reference, template, data in records:
                            entities[reference].append({
                                "time_ms": time_ms,
                                "order": packet_order,
                                "vehicle": extract_model(template, data),
                            })

                    except Exception as error:
                        raise ReplayServiceError(
                            f"ECS: {path.name}, смещение {offset}: {error}"
                        ) from error

        except ReplayServiceError as error:
            raise ReplayServiceError(
                f"Ошибка потока {path.name}: {error}"
            ) from error

        if header["footer_offset"]:
            try:
                footer = decode_footer(raw[header["footer_offset"]:], decoder)
                footers.append((path.name, footer))

            except Exception as error:
                add_error(path.name, header["footer_offset"], "footer", error)

    check(
        bool(footers),
        "Не удалось получить footer. "
        "Проверь наличие всех частей боя и библиотеку wt-blk-pybindings.",
    )

    if strict and errors:
        raise ReplayServiceError(
            "При разборе реплея возникли ошибки:\n" + "\n".join(errors[:5])
        )

    footer = pick_footer(footers)
    matching_info = footer.get("matchingInfo", {})

    if not isinstance(matching_info, dict):
        matching_info = {}

    footer_by_id: dict[str, dict] = {}

    for player in get_footer_players(footer):
        user_id = player.get("userId")

        if user_id is not None:
            footer_by_id[str(user_id)] = player

    names_by_id: dict[str, str] = {}

    identity_events = sorted(
        (
            identity
            for history in identity_history.values()
            for identity in history
        ),
        key=lambda item: item["order"],
    )

    for identity in identity_events:
        name = identity["name"]

        if isinstance(name, str) and name.strip():
            names_by_id[identity["id"]] = name.strip()

    observations_by_id: dict[str, dict[str, int]] = defaultdict(dict)

    unresolved_identity = 0
    unresolved_entity = 0
    unresolved_model = 0

    for event in references:
        identity = find_identity(identity_history.get(event["slot"], []), event)

        if identity is None:
            unresolved_identity += 1
            continue

        entity = find_entity(entities.get(event["reference"], []), event)

        if entity is None:
            unresolved_entity += 1
            continue

        normalized = normalize_vehicle_ids([entity.get("vehicle")])

        if not normalized:
            unresolved_model += 1
            continue

        vehicle = normalized[0]
        user_id = identity["id"]

        observations = observations_by_id[user_id]
        first_seen = event["time_ms"]
        previous_seen = observations.get(vehicle)

        if previous_seen is None or first_seen < previous_seen:
            observations[vehicle] = first_seen

    logger.debug(
        "Разбор событий завершён: references=%s, entity_refs=%s, "
        "без игрока=%s, без ECS-объекта=%s, без модели=%s",
        len(references), len(entities),
        unresolved_identity, unresolved_entity, unresolved_model,
    )

    user_ids = list(footer_by_id)

    for user_id in names_by_id:
        if user_id not in footer_by_id:
            user_ids.append(user_id)

    players = []

    for user_id in user_ids:
        footer_player = footer_by_id.get(user_id, {})
        info = matching_info.get(user_id, {})

        if not isinstance(info, dict):
            info = {}

        crafts = array_values(info.get("crafts_info", {}))
        lineup = normalize_vehicle_ids([craft.get("name") for craft in crafts])

        observed = [
            vehicle
            for vehicle, _ in sorted(
                observations_by_id.get(user_id, {}).items(),
                key=lambda item: (item[1], item[0]),
            )
        ]

        name = footer_player.get("name")

        if not isinstance(name, str) or not name.strip():
            name = names_by_id.get(user_id, "")

        if isinstance(name, str):
            name = name.strip()

        if not observed:
            if lineup:
                logger.warning(
                    "Использованная техника не найдена: user_id=%s, name=%r. "
                    "Для вывода беру первую из сетапа: %s",
                    user_id, name, lineup[0],
                )
            else:
                logger.warning(
                    "Нет данных о технике: user_id=%s, name=%r",
                    user_id, name,
                )

        players.append({
            "id": user_id,
            "name": name,
            "clan": footer_player.get("clanTag", ""),
            "lineup": lineup,
            "observed": observed,
        })

    check(bool(players), "В реплее не найдены игроки")

    return players


def format_player_vehicles(player: dict) -> str:
    observed = normalize_vehicle_ids(player.get("observed", []))
    lineup = normalize_vehicle_ids(player.get("lineup", []))

    if observed:
        main = ", ".join(observed)
        observed_set = set(observed)

        extra = [
            vehicle
            for vehicle in lineup
            if vehicle not in observed_set
        ]

        if extra:
            return f"{main} ({', '.join(extra)})"

        return main

    if lineup:
        main = lineup[0]
        extra = lineup[1:]

        if extra:
            return f"Из сетапа: {main} ({', '.join(extra)})"

        return f"Из сетапа: {main}"

    return "Нет данных о технике"


def build_clan_result(players: list[dict]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    skipped = []

    for player in players:
        clan = player.get("clan")
        name = player.get("name")

        clan = clan.strip() if isinstance(clan, str) else ""
        name = name.strip() if isinstance(name, str) else ""

        if not clan:
            skipped.append(name or str(player.get("id", "?")))
            continue

        check(bool(name), f"Не найден ник игрока {player.get('id', '?')}.")

        members = result.setdefault(clan, {})

        check(
            name not in members,
            f"Повторяющийся ник {name!r} в клане {clan!r}.",
        )

        members[name] = format_player_vehicles(player)

    check(
        len(result) == 2,
        "Ожидалось ровно два клана, "
        f"найдено: {len(result)}. "
        f"Теги: {', '.join(result) or 'нет'}. "
        "Игроки без найденного тега: "
        f"{', '.join(skipped) or 'нет'}.",
    )

    if skipped:
        logger.warning(
            "В результат не включены игроки без тега: %s",
            ", ".join(skipped),
        )

    return {
        clan: dict(sorted(
            members.items(),
            key=lambda item: item[0].casefold(),
        ))
        for clan, members in sorted(
            result.items(),
            key=lambda item: item[0].casefold(),
        )
    }


def parse_wrpl_files(
    files: Iterable[str | Path] | str | Path,
    *,
    session_id: str | None = None,
    strict: bool = True,
) -> dict[str, dict[str, str]]:
    try:
        paths = select_replay_files(files, session_id=session_id)
        players = parse_replay_parts(paths, strict=strict)

        return build_clan_result(players)

    except ReplayServiceError:
        raise

    except Exception as error:
        raise ReplayServiceError(
            f"Не удалось обработать реплей: {error}"
        ) from error


def parse_wrpl_folder(
    source: str | Path,
    *,
    session_id: str | None = None,
    strict: bool = True,
) -> dict[str, dict[str, str]]:
    try:
        source = Path(source).expanduser().resolve()

        check(source.is_dir(), f"Не найдена папка: {source}")

        files = [
            path
            for path in source.iterdir()
            if path.is_file() and path.suffix.lower() == ".wrpl"
        ]

        check(bool(files), f"В папке нет файлов .wrpl: {source}")

        return parse_wrpl_files(files, session_id=session_id, strict=strict)

    except ReplayServiceError:
        raise

    except Exception as error:
        raise ReplayServiceError(
            f"Не удалось обработать папку: {error}"
        ) from error


def parse_wrpl_json(
    source: Iterable[str | Path] | str | Path,
    *,
    session_id: str | None = None,
    strict: bool = True,
) -> str:
    if isinstance(source, (str, Path)) and Path(source).expanduser().is_dir():
        result = parse_wrpl_folder(source, session_id=session_id, strict=strict)
    else:
        result = parse_wrpl_files(source, session_id=session_id, strict=strict)

    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))