-- 0013_wifi: phones paired for Wi-Fi adb (spec 6), reconnected at launch.
CREATE TABLE wifi_phone (
    name TEXT NOT NULL,            -- mDNS name of the phone's connect service ('' if unknown)
    address TEXT NOT NULL,         -- last host:port that connected
    paired_at INTEGER NOT NULL,
    last_connected INTEGER,
    PRIMARY KEY (name, address)
);
