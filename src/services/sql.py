import sqlite3
import os
from src.constants import DB_PATH
import json as json_lib
from contextlib import closing

def init_sql(): 
    os.makedirs(DB_PATH.parent, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sqb_teams (
            tagl TEXT NOT NULL,
            player_vehicle TEXT NOT NULL,
            players TEXT NOT NULL,
            time INTEGER NOT NULL,
            PRIMARY KEY (players)
            )
        """)
        


def add_new_teams_sql(tag: str, player_vehicle: dict, players: list[str]):

    player_vehicle_text = json_lib.dumps(player_vehicle)
    players_text = json_lib.dumps(sorted(players))

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO sqb_teams (tagl, player_vehicle, players, time)
                VALUES (?, ?, ?, ?)
            """, (tag.lower(), player_vehicle_text, players_text, 0,))





def update_teams_sql(player_vehicle: dict, players: list[str]):
    player_vehicle_str = json_lib.dumps(player_vehicle)
    players_str = json_lib.dumps(sorted(players))

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sqb_teams
                SET player_vehicle = ?, time = 0
                WHERE players = ?
            """, (player_vehicle_str, players_str))
        





def update_time_sql(time: int):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sqb_teams
                SET time = time + ?
            """, (time,))




# убирает старые записи
def filter_sql(time: int):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM sqb_teams
                WHERE time >= ?
            """, (time,))





def select_sql(tag: str) -> list[dict]:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        cursor = conn.execute("""
            SELECT player_vehicle, time
            FROM sqb_teams
            WHERE tagl = ?
        """, (tag.lower(),))

        rows = cursor.fetchall()

    return [{
            "player_vehicle": json_lib.loads(row[0]),
            "time": row[1]
            } for row in rows]


def select_allplayers_sql() -> list[list[str]]:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        rows = conn.execute("""
            SELECT players
            FROM sqb_teams
        """).fetchall()

    teams = []

    for row in rows:
        team = json_lib.loads(row[0])

        if not isinstance(team, list) or any(not isinstance(name, str) for name in team):
            raise ValueError(f"Некорректный состав в базе: {row[0]!r}")

        teams.append(team)

    return teams

