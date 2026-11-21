from __future__ import annotations

import json as json_lib
import sqlite3
from contextlib import closing

from src.constants import DB_PATH


def normalize_tag(tag: str) -> str:
    tag = tag.strip()

    while tag and not tag[0].isalnum():
        tag = tag[1:]

    while tag and not tag[-1].isalnum():
        tag = tag[:-1]

    return tag.lower()


def is_same_team(
    old_players: list[str] | tuple[str, ...],
    new_players: list[str] | tuple[str, ...],
) -> bool:
    return len(set(old_players) & set(new_players)) >= 4


def init_sql():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")

        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sqb_teams (
                    tagl TEXT NOT NULL,
                    player_vehicle TEXT NOT NULL,
                    players TEXT NOT NULL,
                    time INTEGER NOT NULL,
                    PRIMARY KEY (players)
                )
            """)


def save_team_sql(tag: str, player_vehicle: dict[str, str]) -> dict:

    tag = normalize_tag(tag)

    if not tag:
        raise ValueError("Пустой тег клана после нормализации")

    new_players = sorted(player_vehicle)
    new_set = set(new_players)

    if not new_players:
        raise ValueError("Нельзя сохранить пустой состав")

    players_text = json_lib.dumps(new_players)
    vehicle_text = json_lib.dumps(player_vehicle)

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            rows = conn.execute("""
                SELECT rowid, players, time
                FROM sqb_teams
                WHERE tagl = ?
                ORDER BY time ASC, rowid ASC
            """, (tag,)).fetchall()

            best_rowid = None
            best_common = -1
            best_old_players = None

            for rowid, saved_players_text, age in rows:
                old_players = json_lib.loads(saved_players_text)

                if (
                    not isinstance(old_players, list)
                    or any(
                        not isinstance(name, str)
                        for name in old_players
                    )
                ):
                    raise ValueError(
                        f"Некорректный состав в базе: "
                        f"{saved_players_text!r}"
                    )

                if not is_same_team(old_players, new_players):
                    continue

                common = len(set(old_players) & new_set)

                if common > best_common:
                    best_rowid = rowid
                    best_common = common
                    best_old_players = old_players


            exact_row = conn.execute("""
                SELECT rowid, tagl
                FROM sqb_teams
                WHERE players = ?
            """, (players_text,)).fetchone()

            if exact_row is not None:
                exact_rowid, exact_tag = exact_row

                if exact_tag != tag:
                    raise ValueError(
                        "Такой список игроков уже записан "
                        f"для другого клана: {exact_tag!r}. "
                        "Текущий PRIMARY KEY(players) "
                        "не позволяет сохранить его повторно."
                    )

                best_rowid = exact_rowid
                best_common = len(new_set)
                best_old_players = new_players

            if best_rowid is not None:
                conn.execute("""
                    UPDATE sqb_teams
                    SET tagl = ?,
                        player_vehicle = ?,
                        players = ?,
                        time = 0
                    WHERE rowid = ?
                """, (
                    tag,
                    vehicle_text,
                    players_text,
                    best_rowid,
                ))

                result = {
                    "action": "updated",
                    "common": best_common,
                    "old_players": best_old_players,
                    "new_players": new_players,
                }

            else:
                conn.execute("""
                    INSERT INTO sqb_teams (
                        tagl,
                        player_vehicle,
                        players,
                        time
                    )
                    VALUES (?, ?, ?, 0)
                """, (
                    tag,
                    vehicle_text,
                    players_text,
                ))

                result = {
                    "action": "added",
                    "common": 0,
                    "old_players": None,
                    "new_players": new_players,
                }

    return result



def add_new_teams_sql(tag: str, player_vehicle: dict, players: list[str],):
    tag = normalize_tag(tag)
    player_vehicle_text = json_lib.dumps(player_vehicle)
    players_text = json_lib.dumps(sorted(players))

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO sqb_teams (
                    tagl,
                    player_vehicle,
                    players,
                    time
                )
                VALUES (?, ?, ?, 0)
            """, (
                tag,
                player_vehicle_text,
                players_text,
            ))


def update_teams_sql(player_vehicle: dict, players: list[str],):
    player_vehicle_text = json_lib.dumps(player_vehicle)
    players_text = json_lib.dumps(sorted(players))

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                UPDATE sqb_teams
                SET player_vehicle = ?, time = 0
                WHERE players = ?
            """, (
                player_vehicle_text,
                players_text,
            ))


def update_time_sql(time: int):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                UPDATE sqb_teams
                SET time = time + ?
            """, (time,))


def filter_sql(time: int):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                DELETE FROM sqb_teams
                WHERE time >= ?
            """, (time,))


def select_sql(tag: str) -> list[dict]:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute("""
            SELECT player_vehicle, time
            FROM sqb_teams
            WHERE tagl = ?
            ORDER BY time ASC
        """, (normalize_tag(tag),)).fetchall()

    return [
        {
            "player_vehicle": json_lib.loads(row[0]),
            "time": row[1],
        }
        for row in rows
    ]


def select_allplayers_sql() -> list[list[str]]:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute("""
            SELECT players
            FROM sqb_teams
        """).fetchall()

    teams = []

    for row in rows:
        team = json_lib.loads(row[0])

        if (
            not isinstance(team, list)
            or any(not isinstance(name, str) for name in team)
        ):
            raise ValueError(
                f"Некорректный состав в базе: {row[0]!r}"
            )

        teams.append(team)

    return teams