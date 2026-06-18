"""
Quick connection test - run this before starting the app.
Usage:  python test_db_connection.py
"""

import os
import sys

# override these if your setup is different
DB_HOST     = os.environ.get("DB_HOST",     "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_NAME     = os.environ.get("DB_NAME",     "criticlens")
DB_USER     = os.environ.get("DB_USER",     "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

print(f"Connecting to MySQL at {DB_HOST}:{DB_PORT} as {DB_USER} → database '{DB_NAME}'")

try:
    import pymysql
    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        connect_timeout=5,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]
    conn.close()

    print(f"Connected OK - MySQL version: {version}")
    if tables:
        print(f"Existing tables: {tables}")
    else:
        print("No tables yet (schema will be created on first startup)")
    print("\nConnection test PASSED")

except ImportError:
    print("ERROR: PyMySQL not installed. Run: pip install PyMySQL")
    sys.exit(1)
except pymysql.err.OperationalError as e:
    print(f"\nConnection FAILED: {e}")
    print("\nThings to check:")
    print("  1. Is MySQL running? (check MySQL Workbench -> Server Status)")
    print(f"  2. Does the database '{DB_NAME}' exist?")
    print("     Run in MySQL Workbench: CREATE DATABASE IF NOT EXISTS criticlens;")
    print("  3. Is the password correct?")
    print("     Set env var:  $env:DB_PASSWORD = 'yourpassword'")
    print(f"  4. Is the port correct? (current: {DB_PORT})")
    print("     Check MySQL Workbench -> Options File -> port")
    sys.exit(1)
