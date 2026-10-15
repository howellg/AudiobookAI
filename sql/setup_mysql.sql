-- Run in VS Code SQL Workbench as a MySQL administrator.
-- BEFORE running: change the placeholder password below.
-- 127.0.0.1 matches the GUI's default TCP host.
-- Privileges are restricted to one application-specific database.

CREATE DATABASE IF NOT EXISTS audiobookai
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'audiobookai'@'127.0.0.1'
  IDENTIFIED BY 'REPLACE_THIS_WITH_YOUR_OWN_STRONG_PASSWORD';

GRANT ALL PRIVILEGES ON audiobookai.* TO 'audiobookai'@'127.0.0.1';

-- If the user already exists, CREATE USER IF NOT EXISTS won't change its
-- password. Change it explicitly if needed:
-- ALTER USER 'audiobookai'@'127.0.0.1' IDENTIFIED BY 'YOUR_NEW_PASSWORD';
-- No FLUSH PRIVILEGES necessary after GRANT in modern MySQL.
