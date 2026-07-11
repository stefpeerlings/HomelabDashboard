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

-- Toegang vanaf het homelab-netwerk (pas aan indien nodig)
-- Voor toegang vanaf elke host: 'homelab_dashboard'@'%'
CREATE USER IF NOT EXISTS 'homelab_dashboard'@'10.0.%' IDENTIFIED BY 'CHANGE_ME';
CREATE USER IF NOT EXISTS 'homelab_dashboard'@'localhost' IDENTIFIED BY 'CHANGE_ME';

GRANT ALL PRIVILEGES ON homelab_dashboard.* TO 'homelab_dashboard'@'10.0.%';
GRANT ALL PRIVILEGES ON homelab_dashboard.* TO 'homelab_dashboard'@'localhost';

FLUSH PRIVILEGES;