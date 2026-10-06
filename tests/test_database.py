from app.database.connection import DatabaseConnection



db = DatabaseConnection()


with db.connect() as conn:

    print(
        "Database Connected"
    )