"""Preview or apply structured dates found in legacy calendar text."""
import argparse

from app import calendar_dates, db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="записати дати в PostgreSQL")
    args = parser.parse_args()
    connection = db.connect(readonly=not args.apply)
    try:
        rows = calendar_dates.preview_unknown_dates(connection)
        print(f"Розпізнано дат: {len(rows)}")
        for row in rows[:20]:
            print(row["id"], row["date_precision"], row["starts_at"], row["ends_at"] or "—", row["title"][:90])
        if len(rows) > 20:
            print(f"... ще {len(rows) - 20}")
        if args.apply:
            calendar_dates.apply_unknown_dates(connection)
            print("Дати записано; історію змін збережено.")
        else:
            print("Це лише перегляд. Для запису додайте --apply або скористайтеся кнопкою в панелі.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
