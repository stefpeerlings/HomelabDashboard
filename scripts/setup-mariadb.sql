-- Homelab Dashboard — MariaDB database + gebruiker
-- Vervang CHANGE_ME door een sterk wachtwoord vóór je dit uitvoert.
--
-- Uitvoeren op de MariaDB-host (als root):
--   mysql -u root -p < setup-mariadb.sql
--
-- Of via het interactieve script:
--   bash scripts/setup-mariadb.sh

CREATE DATABASE IF NOT EXISTS homelab_dashboard
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

-- Toegang vanaf je LAN (pas subnet aan: 192.168.%, 10.0.%, of '%')
CREATE USER IF NOT EXISTS 'homelab_dashboard'@'192.168.%' IDENTIFIED BY 'CHANGE_ME';
CREATE USER IF NOT EXISTS 'homelab_dashboard'@'localhost' IDENTIFIED BY 'CHANGE_ME';

GRANT ALL PRIVILEGES ON homelab_dashboard.* TO 'homelab_dashboard'@'192.168.%';
GRANT ALL PRIVILEGES ON homelab_dashboard.* TO 'homelab_dashboard'@'localhost';

FLUSH PRIVILEGES;