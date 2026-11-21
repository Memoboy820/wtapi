from __future__ import annotations

import asyncio
from asyncio import to_thread
import json
import logging
import shutil

from src.constants import CHECK_INTERVAL, logger, REPLAY_FOLDER
from src.services.requests import check_replays, load_state
from src.services.sql import (
    init_sql,
    save_team_sql,
    filter_sql,
    update_time_sql,
    select_allplayers_sql,
)


# Сейчас один час. Для 30 минут поставь 30 * 60.
TEAM_MAX_AGE_SECONDS = 3600


async def process_saved_results(
    *,
    delete_replays: bool = True,
) -> dict[str, dict]:
    replay_root = REPLAY_FOLDER.resolve()
    results = {}

    logger.info(
        "Проверка результатов: путь=%s, удаление=%s",
        replay_root,
        delete_replays,
    )

    if not replay_root.is_dir():
        logger.warning(
            "Папка реплеев не существует: %s",
            replay_root,
        )
        return results

    folders_seen = 0
    results_found = 0
    processed = 0
    deleted = 0
    failed = 0

    for replay_folder in sorted(replay_root.iterdir()):
        if replay_folder.is_symlink():
            logger.warning(
                "Пропускаю символическую ссылку: %s",
                replay_folder,
            )
            continue

        if not replay_folder.is_dir():
            continue

        folders_seen += 1
        result_path = replay_folder / "result.json"

        if not result_path.is_file():
            logger.warning(
                "Пропускаю бой %s: нет файла %s",
                replay_folder.name,
                result_path,
            )
            continue

        results_found += 1

        try:
            with result_path.open("r", encoding="utf-8") as file:
                result = json.load(file)

            if not isinstance(result, dict) or not result:
                raise ValueError(
                    "В result.json ожидался непустой словарь кланов"
                )

            # Проверяем весь результат до сохранения.
            for tag, player_vehicle in result.items():
                if not isinstance(tag, str) or not tag.strip():
                    raise ValueError(
                        f"Некорректный тег клана: {tag!r}"
                    )

                if (
                    not isinstance(player_vehicle, dict)
                    or not player_vehicle
                ):
                    raise ValueError(
                        f"У клана {tag!r} ожидался непустой "
                        "словарь игроков с техникой"
                    )

                for name, vehicle in player_vehicle.items():
                    if (
                        not isinstance(name, str)
                        or not name.strip()
                        or not isinstance(vehicle, str)
                    ):
                        raise ValueError(
                            "Некорректные данные игрока "
                            f"или техники у клана {tag!r}"
                        )

            for tag, player_vehicle in result.items():
                saved = await to_thread(
                    save_team_sql,
                    tag=tag,
                    player_vehicle=player_vehicle,
                )

                if saved["action"] == "updated":
                    old_players = set(saved["old_players"])
                    new_players = set(saved["new_players"])

                    logger.info(
                        "Бой %s: обновлён состав клана %s, "
                        "общих игроков=%s, ушли=%s, пришли=%s",
                        replay_folder.name,
                        tag,
                        saved["common"],
                        sorted(old_players - new_players),
                        sorted(new_players - old_players),
                    )
                else:
                    logger.info(
                        "Бой %s: добавлен состав клана %s, "
                        "игроков=%s",
                        replay_folder.name,
                        tag,
                        len(saved["new_players"]),
                    )

            results[replay_folder.name] = result
            processed += 1

        except Exception:
            failed += 1
            logger.exception(
                "Не удалось обработать бой %s. "
                "Папка сохранена: %s",
                replay_folder.name,
                replay_folder,
            )
            continue

        if delete_replays:
            try:
                await to_thread(shutil.rmtree, replay_folder)
                deleted += 1

                logger.info(
                    "Удалена папка боя: %s",
                    replay_folder,
                )
            except OSError:
                logger.exception(
                    "Данные боя %s сохранены, "
                    "но папку удалить не удалось: %s",
                    replay_folder.name,
                    replay_folder,
                )
        else:
            logger.info(
                "Бой %s сохранён. Удаление выключено.",
                replay_folder.name,
            )

    logger.info(
        "Итог: папок=%s, result.json=%s, сохранено=%s, "
        "ошибок обработки=%s, удалено=%s",
        folders_seen,
        results_found,
        processed,
        failed,
        deleted,
    )

    return results


async def task() -> None:
    await to_thread(init_sql)
    state = await to_thread(load_state)

    logger.info(
        "Сборщик запущен. Папка реплеев: %s",
        REPLAY_FOLDER.resolve(),
    )

    loop = asyncio.get_running_loop()
    last_aged = loop.time()

    async def age_teams() -> None:
        nonlocal last_aged

        elapsed = int(loop.time() - last_aged)

        if elapsed > 0:
            await to_thread(update_time_sql, elapsed)
            last_aged += elapsed

    while True:
        try:
            await age_teams()

            # Удаляем просроченные записи до поиска совпадений.
            await to_thread(filter_sql, TEAM_MAX_AGE_SECONDS)

            try:
                logger.info(
                    "Начинаю проверку и обработку реплеев"
                )

                await check_replays(state)

                logger.info(
                    "check_replays завершён. "
                    "Перехожу к сохранению JSON и очистке."
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Ошибка проверки реплеев. "
                    "Попробую сохранить уже готовые результаты."
                )

            # Учитываем время, потраченное на загрузку,
            # до сохранения новых результатов с time = 0.
            await age_teams()
            await to_thread(filter_sql, TEAM_MAX_AGE_SECONDS)

            results = await process_saved_results(
                delete_replays=True,
            )

            logger.info(
                "Обработка JSON закончена. Боёв=%s, теги=%s",
                len(results),
                sorted({
                    tag
                    for result in results.values()
                    for tag in result
                }),
            )

            players = await to_thread(select_allplayers_sql)

            logger.info(
                "Составов в SQL: %s",
                len(players),
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Ошибка обработки результатов или SQLite"
            )

        logger.info(
            "Следующая проверка через %s секунд",
            CHECK_INTERVAL,
        )

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        asyncio.run(task())
    except KeyboardInterrupt:
        pass